"""Service code as an evidence source (stage 2 of the plan): SQL found in JS/TS/Python code.

CONTRACT (implemented by the build step):

    extract_sql(text, path='<string>', language=None, dialect=None) -> list[QueryRecord]
        Finds string literals ('...', "...", `...` template literals, Python triple-quoted strings)
        whose content starts with SELECT or WITH (case-insensitive, after whitespace) and parses as SQL.
        Template placeholders ${...} and Python {...}/%s/? parameters become literal placeholders so the
        statement still parses. Returns QueryRecord(kind=EvidenceKind.CODE, ref='<path>:<line>').
    scan_repository(root, include=DEFAULT_INCLUDE, exclude=DEFAULT_EXCLUDE, dialect=None) -> list[QueryRecord]
    orm_queries(...)          planned (ORM such as Prisma, SQLAlchemy, Django)
    lift_js_computation(...)  planned (static analysis of UI JavaScript computations)

How the extraction works
------------------------
1. Lexing. A small lexer per language family returns the string literals of a source file and skips
   comments (and, in JavaScript, regular-expression literals), so SQL inside a comment is ignored and a
   quote inside a comment or a regex does not derail the scan.
   JavaScript/TypeScript: '...', "..." and `...` template literals (tagged or not, nested ${...} included).
   Python: every string prefix (r, b, u, f, t and their combinations), single- or triple-quoted, with
   f-string {...} fields (format specs and nested quotes included).
   A quoted string that is not closed on its own line is skipped, so the scan resynchronises at once.
2. Concatenation. Literals that form one value are joined: Python implicit concatenation
   (``"SELECT a " "FROM t"``) and, in both languages, ``+`` between two literals.
   Bare Python string statements (docstrings) are skipped.
3. Parameters. Every ${...} and f-string {...} field becomes the placeholder ``?`` (:data:`PLACEHOLDER`);
   inside a quoted SQL token the whole token does (``name = '${n}'`` -> ``name = ?``, ``LIKE '%${q}%'`` ->
   ``LIKE ?``), because its value is only known at run time and must not be read as the constant ``'?'``.
   Outside SQL quotes, the DB-API styles ``%s``, ``%(name)s`` and ``?NNN``, ``str.format`` fields
   (``{}``, ``{name}``) in plain Python strings, and ``${...}`` written as text in plain JavaScript strings
   also become ``?``; ``%%`` becomes ``%``. ``?``, ``$1``, ``:name`` and ``@name`` are kept, because sqlglot
   parses them already.
4. Validation. The text must start with SELECT or WITH, and sqlglot must parse it into queries (SELECT,
   WITH ... SELECT, UNION, ...) that read at least one named table. A statement whose structure is built
   at run time is skipped: one where a placeholder stands for a table (``FROM ${table}``), an alias or a
   column name. ``FROM orders ${where}`` is an example, because sqlglot reads the interpolated clause as
   an alias. Placeholders in value positions are fine. The table rule also skips UI text such as
   "Select a file" (which sqlglot reads as ``SELECT a AS file``) and table-less probes such as ``SELECT 1``.
   Neither kind can support a definition.

Each query becomes ``QueryRecord(sql, id='<path>:<line>:<column>', kind=EvidenceKind.CODE,
ref='<path>:<line>')``, where line and column (1-based) are where the literal starts (for a Python
literal, its prefix). ``sql`` is the literal's value with parameters replaced, stripped of surrounding
whitespace. If a literal holds several statements, each qualifying query gets its own record with the SQL
re-rendered by sqlglot and ``.1``, ``.2``, ... appended to the id. Records are in source order, and
:func:`scan_repository` orders files by their path relative to the root (POSIX form).
"""
from __future__ import annotations

import os
import re
import unicodedata
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

import sqlglot
from sqlglot import exp

from ._unimplemented import planned
from .induce import QueryRecord
from .model import EvidenceKind

DEFAULT_INCLUDE = ('**/*.js', '**/*.jsx', '**/*.ts', '**/*.tsx', '**/*.mjs', '**/*.py')
DEFAULT_EXCLUDE = ('**/node_modules/**', '**/.git/**', '**/dist/**', '**/build/**', '**/.venv/**', '**/venv/**')

PLACEHOLDER = '?'
"""Replaces every parameter and interpolation; sqlglot parses it as a placeholder in every dialect."""

_LANGUAGE_BY_EXTENSION = {
    '.py': 'python', '.pyi': 'python', '.pyw': 'python',
    '.js': 'javascript', '.jsx': 'javascript', '.mjs': 'javascript', '.cjs': 'javascript',
    '.ts': 'typescript', '.tsx': 'typescript', '.mts': 'typescript', '.cts': 'typescript',
}
_LANGUAGE_ALIASES = {
    'python': 'python', 'py': 'python',
    'javascript': 'javascript', 'js': 'javascript', 'jsx': 'javascript', 'mjs': 'javascript',
    'cjs': 'javascript', 'node': 'javascript',
    'typescript': 'typescript', 'ts': 'typescript', 'tsx': 'typescript', 'mts': 'typescript', 'cts': 'typescript',
}

_TEXT, _PARAM = 'text', 'param'
_BOM = chr(0xFEFF)
_SQL_START = re.compile(r'\s*(?:select|with)\b', re.IGNORECASE)


@dataclass
class _Literal:
    """One string literal: its span in the source and its value as text and parameter parts."""

    start: int                       # offset of the first character (a Python prefix included)
    end: int                         # offset just after the closing quote
    parts: List[Tuple[str, str]]     # (_TEXT, cooked text) or (_PARAM, '')
    style: str = ''                  # 'pyformat' (plain Python), 'jsplain' (plain JavaScript), '' otherwise


# ---------------------------------------------------------------------------------------------- public API
def extract_sql(text: str, path: str = '<string>', language: Optional[str] = None,
                dialect: Optional[str] = None) -> List[QueryRecord]:
    """SQL queries found in the string literals of one source file.

    Args:
        text: the source code.
        path: written into each record's ref (``'<path>:<line>'``) and used to infer the language.
        language: 'python', 'javascript' or 'typescript', or an alias (py, js, jsx, mjs, cjs, ts, tsx, ...).
            None infers it from the extension of ``path``. Unknown extensions use the JavaScript lexer,
            which also fits other C-family languages.
        dialect: sqlglot dialect used for the parse check (None = sqlglot's default dialect).

    Returns:
        QueryRecords in source order, with kind=EvidenceKind.CODE, ref='<path>:<line>' (the 1-based line
        where the literal starts) and id='<path>:<line>:<column>'.

    Raises:
        TypeError: ``text`` is not a string.
        ValueError: unknown ``language`` or ``dialect``.
    """
    if not isinstance(text, str):
        raise TypeError(f'text must be str, not {type(text).__name__}')
    lang = _resolve_language(path, language)
    _check_dialect(dialect)
    source = text.replace('\r\n', '\n').replace('\r', '\n')
    if source.startswith(_BOM):
        source = source[1:]
    groups = _scan_python(source) if lang == 'python' else _scan_js(source)
    records: List[QueryRecord] = []
    line_starts: Optional[List[int]] = None
    for group in groups:
        if not _starts_like_sql(group):
            continue
        queries = _queries(_group_sql(group).strip(), dialect)
        if not queries:
            continue
        if line_starts is None:
            line_starts = _line_starts(source)
        line, column = _position(line_starts, group[0].start)
        ref = f'{path}:{line}'
        for k, sql in enumerate(queries, 1):
            rid = f'{ref}:{column}' if len(queries) == 1 else f'{ref}:{column}.{k}'
            records.append(QueryRecord(sql=sql, id=rid, kind=EvidenceKind.CODE, ref=ref))
    return records


def scan_repository(root: Union[str, Path], include: Iterable[str] = DEFAULT_INCLUDE,
                    exclude: Iterable[str] = DEFAULT_EXCLUDE, dialect: Optional[str] = None) -> List[QueryRecord]:
    """SQL queries found in the source files under ``root`` (see :func:`extract_sql`).

    Args:
        root: a directory (or a single file, which is then scanned whatever its name).
        include, exclude: glob patterns matched against each file's path relative to ``root`` in POSIX
            form. ``**`` spans directories (``**/x`` also matches ``x`` at the top) and ``*`` stays within
            one path segment. A file is scanned if it matches an include pattern and no exclude pattern.
            Directories matched by an exclude pattern ending in ``/**`` are not entered.
            A single string counts as one pattern.
        dialect: sqlglot dialect for the parse check.

    Returns:
        Records ordered by relative path, then by position in the file. Each ref is
        ``'<relative/posix/path>:<line>'``. Symlinked directories are not followed, and unreadable files
        and binary files (those containing NUL bytes) are skipped. Text is decoded as UTF-8, with
        undecodable bytes replaced.

    Raises:
        FileNotFoundError: ``root`` does not exist.
        ValueError: unknown ``dialect``.
    """
    base = Path(root)
    if not base.exists():
        raise FileNotFoundError(f'no such file or directory: {root}')
    _check_dialect(dialect)
    if base.is_file():
        files = [(base.name, base)]
    else:
        files = _repository_files(base, _compile_globs(include), _compile_globs(exclude))
    records: List[QueryRecord] = []
    for rel, file_path in files:
        text = _read_source(file_path)
        if text is not None:
            records.extend(extract_sql(text, path=rel, dialect=dialect))
    return records


def orm_queries(*args, **kwargs):
    """Definitions from ORM queries (Prisma, SQLAlchemy, Django ORM, ...). Planned (stage 2)."""
    planned('make.code_orm')


def lift_js_computation(*args, **kwargs):
    """Lift UI JavaScript computations (filter/map/reduce over API data) to definitions. Planned (stage 2)."""
    planned('make.code_js')


# ---------------------------------------------------------------------------------------------- helpers
def _resolve_language(path: str, language: Optional[str]) -> str:
    """Lexer family for ``language`` or, when it is None, for the extension of ``path``."""
    if language is not None:
        key = str(language).strip().lower().lstrip('.')
        if key not in _LANGUAGE_ALIASES:
            raise ValueError(f'unknown language {language!r}; use one of: python, javascript, typescript')
        return _LANGUAGE_ALIASES[key]
    return _LANGUAGE_BY_EXTENSION.get(os.path.splitext(str(path))[1].lower(), 'javascript')


def _check_dialect(dialect: Optional[str]) -> None:
    """Raise ValueError for a dialect sqlglot does not know."""
    if dialect is not None:
        sqlglot.Dialect.get_or_raise(dialect)


def _line_end(text: str, i: int) -> int:
    """Offset of the newline that ends the line containing ``i`` (or the end of the text)."""
    j = text.find('\n', i)
    return len(text) if j < 0 else j


def _line_starts(text: str) -> List[int]:
    return [0] + [m.end() for m in re.finditer('\n', text)]


def _position(line_starts: List[int], offset: int) -> Tuple[int, int]:
    """1-based (line, column) of an offset."""
    line = bisect_right(line_starts, offset)
    return line, offset - line_starts[line - 1] + 1


def _is_hex(s: str, width: int) -> bool:
    return len(s) == width and all(c in '0123456789abcdefABCDEF' for c in s)


def _fix_surrogates(s: str) -> str:
    """Join escaped UTF-16 surrogate pairs and replace lone surrogates (they cannot be encoded)."""
    if not any(0xD800 <= ord(c) <= 0xDFFF for c in s):
        return s
    return s.encode('utf-16', 'surrogatepass').decode('utf-16', 'replace')


# ------------------------------------------------------------------------------------- JavaScript lexer
_JS_MAIN = re.compile(r'[\'"`/]')
_JS_STOP_QUOTED = {"'": re.compile(r"[\\'\n]"), '"': re.compile(r'[\\"\n]')}
_JS_STOP_TEMPLATE = re.compile(r'[\\`$]')
_JS_STOP_EXPR = re.compile(r'[{}\'"`/]')
_JS_STOP_REGEX = re.compile(r'[\\/\[\]\n]')
# A '/' starts a regex after these characters or keywords; otherwise it divides. '}' and '<' are left out on
# purpose: in JSX, "{x} />" and "</p>" are far more common than a regex after a block.
_JS_REGEX_AFTER = frozenset('(,=:[!&|?{;+-*%>~^')
_JS_REGEX_KEYWORDS = frozenset({'return', 'typeof', 'instanceof', 'in', 'of', 'new', 'delete', 'void', 'throw',
                                'case', 'do', 'else', 'yield', 'await'})
_JS_ESCAPES = {'n': '\n', 't': '\t', 'r': '\r', 'b': '\b', 'f': '\f', 'v': '\v', '0': '\0',
               '\n': '', chr(0x2028): '', chr(0x2029): ''}     # line continuations


def _scan_js(text: str) -> List[List[_Literal]]:
    """String-literal groups (concatenations) of JavaScript/TypeScript source, in source order."""
    groups: List[List[_Literal]] = []
    i = 0
    while True:
        m = _JS_MAIN.search(text, i)
        if m is None:
            return groups
        i = m.start()
        if text[i] == '/':
            i = _js_skip_slash(text, i)
            continue
        lit = _read_js_literal(text, i)
        if lit is None:              # not closed: treat the quote as a stray character
            i += 1
            continue
        group = _js_chain(text, lit)
        groups.append(group)
        i = group[-1].end


def _js_skip_slash(text: str, i: int) -> int:
    """Skip a comment or regex literal starting at the '/' at ``i``; return where scanning resumes."""
    nxt = text[i + 1:i + 2]
    if nxt == '/':
        return _line_end(text, i)
    if nxt == '*':
        j = text.find('*/', i + 2)
        return len(text) if j < 0 else j + 2
    if _js_regex_allowed(text, i):
        j = _skip_js_regex(text, i)
        if j > 0:
            return j
    return i + 1


def _js_regex_allowed(text: str, i: int) -> bool:
    """True if a '/' at ``i`` can start a regex literal (judged from the previous significant token)."""
    j = i - 1
    while j >= 0 and text[j] in ' \t\n\f\v':
        j -= 1
    if j < 0:
        return True
    c = text[j]
    if c in _JS_REGEX_AFTER:
        return True
    if c.isalnum() or c in '_$':
        k = j
        while k >= 0 and (text[k].isalnum() or text[k] in '_$'):
            k -= 1
        return text[k + 1:j + 1] in _JS_REGEX_KEYWORDS
    return False


def _skip_js_regex(text: str, i: int) -> int:
    """End offset (after the flags) of the regex literal at ``i``, or -1 if it is not closed on its line."""
    in_class = False
    j = i + 1
    while True:
        m = _JS_STOP_REGEX.search(text, j)
        if m is None:
            return -1
        j = m.start()
        c = text[j]
        if c == '\n' or (c == '\\' and text[j + 1:j + 2] in ('', '\n')):
            return -1
        if c == '\\':
            j += 2
            continue
        if c == '[':
            in_class = True
        elif c == ']':
            in_class = False
        elif not in_class:           # the closing '/'
            j += 1
            while j < len(text) and (text[j].isalnum() or text[j] in '_$'):
                j += 1
            return j
        j += 1


def _read_js_literal(text: str, i: int) -> Optional[_Literal]:
    return _read_js_template(text, i) if text[i] == '`' else _read_js_quoted(text, i)


def _read_js_quoted(text: str, i: int) -> Optional[_Literal]:
    """A '...' or "..." literal at ``i``; None if it is not closed on its line."""
    stop = _JS_STOP_QUOTED[text[i]]
    j = i + 1
    while True:
        m = stop.search(text, j)
        if m is None:
            return None
        j = m.start()
        c = text[j]
        if c == '\\':
            j += 2
            continue
        if c == '\n':
            return None
        return _Literal(i, j + 1, [(_TEXT, _unescape_js(text[i + 1:j]))], 'jsplain')


def _read_js_template(text: str, i: int) -> Optional[_Literal]:
    """A `...` template literal at ``i``; each ${...} becomes a parameter part. None if not closed."""
    parts: List[Tuple[str, str]] = []
    seg = j = i + 1
    while True:
        m = _JS_STOP_TEMPLATE.search(text, j)
        if m is None:
            return None
        j = m.start()
        c = text[j]
        if c == '\\':
            j += 2
            continue
        if c == '`':
            parts.append((_TEXT, _unescape_js(text[seg:j])))
            return _Literal(i, j + 1, parts, '')
        if text.startswith('${', j):
            parts.append((_TEXT, _unescape_js(text[seg:j])))
            k = _skip_js_expression(text, j + 2)
            if k < 0:
                return None
            parts.append((_PARAM, ''))
            seg = j = k
            continue
        j += 1


def _skip_js_expression(text: str, i: int) -> int:
    """Offset just after the '}' that closes a ${ expression starting at ``i``, or -1."""
    depth = 0
    while True:
        m = _JS_STOP_EXPR.search(text, i)
        if m is None:
            return -1
        i = m.start()
        c = text[i]
        if c == '{':
            depth += 1
            i += 1
        elif c == '}':
            if depth == 0:
                return i + 1
            depth -= 1
            i += 1
        elif c == '/':
            if text.startswith('//', i):
                i = _line_end(text, i)
            elif text.startswith('/*', i):
                j = text.find('*/', i + 2)
                if j < 0:
                    return -1
                i = j + 2
            else:
                i += 1
        else:
            lit = _read_js_literal(text, i)
            if lit is None:
                if c == '`':
                    return -1
                i += 1
            else:
                i = lit.end


def _js_gap(text: str, i: int) -> int:
    """Skip whitespace and comments."""
    n = len(text)
    while i < n:
        if text[i].isspace():
            i += 1
        elif text.startswith('//', i):
            i = _line_end(text, i)
        elif text.startswith('/*', i):
            j = text.find('*/', i + 2)
            i = n if j < 0 else j + 2
        else:
            break
    return i


def _js_chain(text: str, first: _Literal) -> List[_Literal]:
    """``first`` and the literals joined to it with '+'."""
    group = [first]
    while True:
        j = _js_gap(text, group[-1].end)
        if not text.startswith('+', j) or text[j + 1:j + 2] in ('+', '='):
            return group
        k = _js_gap(text, j + 1)
        if k >= len(text) or text[k] not in '\'"`':
            return group
        nxt = _read_js_literal(text, k)
        if nxt is None:
            return group
        group.append(nxt)


def _unescape_js(s: str) -> str:
    """Value of the text between the quotes of a JavaScript string or template literal."""
    if '\\' not in s:
        return s
    out: List[str] = []
    i, n = 0, len(s)
    while True:
        j = s.find('\\', i)
        if j < 0 or j + 1 >= n:
            out.append(s[i:])
            break
        out.append(s[i:j])
        c = s[j + 1]
        i = j + 2
        if c in _JS_ESCAPES:
            out.append(_JS_ESCAPES[c])
        elif c == 'x' and _is_hex(s[i:i + 2], 2):
            out.append(chr(int(s[i:i + 2], 16)))
            i += 2
        elif c == 'u' and s.startswith('{', i):
            k = s.find('}', i)
            code = s[i + 1:k] if k > 0 else ''
            if code and _is_hex(code, len(code)) and int(code, 16) <= 0x10FFFF:
                out.append(chr(int(code, 16)))
                i = k + 1
            else:
                out.append(c)
        elif c == 'u' and _is_hex(s[i:i + 4], 4):
            out.append(chr(int(s[i:i + 4], 16)))
            i += 4
        else:                        # \' \" \` \$ \\ and unknown escapes: the character itself
            out.append(c)
    return _fix_surrogates(''.join(out))


# ----------------------------------------------------------------------------------------- Python lexer
_PY_MAIN = re.compile(r'[#\'"()\[\]{}]')
_PY_PREFIXES = frozenset({'r', 'u', 'b', 'br', 'rb', 'f', 'fr', 'rf', 't', 'tr', 'rt'})
_PY_STOPS: Dict[Tuple[str, bool, bool], re.Pattern[str]] = {}
_PY_ESCAPES = {'\n': '', '\\': '\\', "'": "'", '"': '"', 'a': '\a', 'b': '\b', 'f': '\f', 'n': '\n',
               'r': '\r', 't': '\t', 'v': '\v'}
_PY_OCTAL = re.compile(r'[0-7]{1,3}')


def _scan_python(text: str) -> List[List[_Literal]]:
    """String-literal groups (concatenations) of Python source in source order, docstrings excluded."""
    groups: List[List[_Literal]] = []
    depth = 0                        # bracket depth: inside brackets a newline does not end the statement
    i = 0
    while True:
        m = _PY_MAIN.search(text, i)
        if m is None:
            return groups
        i = m.start()
        c = text[i]
        if c == '#':
            i = _line_end(text, i)
            continue
        if c in '([{':
            depth += 1
            i += 1
            continue
        if c in ')]}':
            depth = max(0, depth - 1)
            i += 1
            continue
        start, prefix = _py_prefix_before(text, i)
        lit = _read_py_string(text, start, i, prefix)
        if lit is None:              # not closed: treat the quote as a stray character
            i += 1
            continue
        group = _py_chain(text, lit, depth)
        i = group[-1].end
        if depth == 0 and _py_bare_statement(text, group[0].start, i):
            continue                 # a docstring or another string used as a statement
        groups.append(group)


def _py_prefix_before(text: str, qpos: int) -> Tuple[int, str]:
    """(start, prefix) of a string whose quote is at ``qpos``: the identifier right before it if it is a
    string prefix such as 'f' or 'rb', else no prefix."""
    k = qpos
    while k > 0 and (text[k - 1].isalnum() or text[k - 1] == '_'):
        k -= 1
    word = text[k:qpos]
    if word and word.lower() in _PY_PREFIXES:
        return k, word
    return qpos, ''


def _py_string_at(text: str, j: int) -> Optional[Tuple[int, str]]:
    """(quote offset, prefix) if a string literal starts exactly at ``j``."""
    k = j
    while k < len(text) and k - j < 2 and text[k].isalpha():
        k += 1
    if k < len(text) and text[k] in '\'"':
        word = text[j:k]
        if not word or word.lower() in _PY_PREFIXES:
            return k, word
    return None


def _py_stop(quote: str, triple: bool, interp: bool) -> re.Pattern[str]:
    key = (quote, triple, interp)
    if key not in _PY_STOPS:
        chars = '\\\\' + quote + ('' if triple else '\\n') + ('{}' if interp else '')
        _PY_STOPS[key] = re.compile('[' + chars + ']')
    return _PY_STOPS[key]


def _read_py_string(text: str, start: int, qpos: int, prefix: str) -> Optional[_Literal]:
    """The Python string literal whose (prefixed) start is ``start`` and quote is at ``qpos``.

    f-string (and t-string) fields become parameter parts. Returns None if the literal is not closed
    (a single-quoted string must close on its line).
    """
    flags = prefix.lower()
    raw = 'r' in flags
    interp = 'f' in flags or 't' in flags
    quote = text[qpos]
    triple = text.startswith(quote * 3, qpos)
    delim = quote * 3 if triple else quote
    stop = _py_stop(quote, triple, interp)
    parts: List[Tuple[str, str]] = []
    seg = j = qpos + len(delim)
    while True:
        m = stop.search(text, j)
        if m is None:
            return None
        j = m.start()
        c = text[j]
        if c == '\\':
            if not raw and text.startswith('N{', j + 1):     # \N{NAME} is an escape, not a field
                k = text.find('}', j + 3)
                j = j + 2 if k < 0 else k + 1
            else:
                j += 2
            continue
        if c == '\n':                # only searched for in single-quoted strings
            return None
        if c == quote:
            if triple and not text.startswith(delim, j):
                j += 1
                continue
            parts.append((_TEXT, _cook_py(text[seg:j], raw, interp)))
            return _Literal(start, j + len(delim), parts, '' if interp else 'pyformat')
        if c == '{':
            if text.startswith('{{', j):
                j += 2
                continue
            parts.append((_TEXT, _cook_py(text[seg:j], raw, interp)))
            k = _skip_py_field(text, j + 1, quote, triple)
            if k < 0:
                return None
            parts.append((_PARAM, ''))
            seg = j = k
            continue
        j += 2 if text.startswith('}}', j) else 1           # '}}' is a literal brace


def _skip_py_field(text: str, i: int, quote: str, triple: bool) -> int:
    """Offset just after the '}' closing an f-string field that starts at ``i``, or -1.

    Handles nested brackets and strings in the expression, '!r' conversions, and format specs with
    nested fields.
    """
    depth = 0
    spec = False
    n = len(text)
    while i < n:
        c = text[i]
        if c == '\n' and not triple:
            return -1
        if spec:
            if c == '{':
                i = _skip_py_field(text, i + 1, quote, triple)
                if i < 0:
                    return -1
                continue
            if c == '}':
                return i + 1
            if c == quote and (not triple or text.startswith(quote * 3, i)):
                return -1
            i += 1
            continue
        if c in '\'"':
            start, prefix = _py_prefix_before(text, i)
            lit = _read_py_string(text, start, i, prefix)
            if lit is None:
                return -1
            i = lit.end
            continue
        if c in '([{':
            depth += 1
        elif c in ')]':
            depth = max(0, depth - 1)
        elif c == '}':
            if depth == 0:
                return i + 1
            depth -= 1
        elif c == ':' and depth == 0:
            spec = True
        i += 1
    return -1


def _py_gap(text: str, i: int, depth: int) -> int:
    """Skip spaces, comments and line continuations, and newlines when inside brackets."""
    n = len(text)
    while i < n:
        c = text[i]
        if c in ' \t\f':
            i += 1
        elif c == '\\' and text.startswith('\\\n', i):
            i += 2
        elif c == '#':
            i = _line_end(text, i)
        elif c == '\n' and depth > 0:
            i += 1
        else:
            break
    return i


def _py_chain(text: str, first: _Literal, depth: int) -> List[_Literal]:
    """``first`` and the literals joined to it, by adjacency or with '+'."""
    group = [first]
    while True:
        j = _py_gap(text, group[-1].end, depth)
        if text.startswith('+', j) and not text.startswith('+=', j):
            j = _py_gap(text, j + 1, depth)
        hit = _py_string_at(text, j)
        if hit is None:
            return group
        nxt = _read_py_string(text, j, hit[0], hit[1])
        if nxt is None:
            return group
        group.append(nxt)


def _py_bare_statement(text: str, start: int, end: int) -> bool:
    """True if text[start:end] is a whole statement on its own (e.g. a docstring)."""
    j = start - 1
    while j >= 0 and text[j] in ' \t':
        j -= 1
    if j >= 0 and (text[j] != '\n' or (j > 0 and text[j - 1] == '\\')):
        return False                 # something precedes it on its logical line
    k = end
    while k < len(text) and text[k] in ' \t':
        k += 1
    return k >= len(text) or text[k] in '\n#;'


def _cook_py(s: str, raw: bool, interp: bool) -> str:
    """Value of the text of a Python string literal (between fields, for f-strings)."""
    if interp:
        s = s.replace('{{', '{').replace('}}', '}')
    return s if raw else _unescape_py(s)


def _unescape_py(s: str) -> str:
    """Apply Python string escapes; unknown escapes keep their backslash, as in Python."""
    if '\\' not in s:
        return s
    out: List[str] = []
    i, n = 0, len(s)
    while True:
        j = s.find('\\', i)
        if j < 0 or j + 1 >= n:
            out.append(s[i:])
            break
        out.append(s[i:j])
        c = s[j + 1]
        i = j + 2
        if c in _PY_ESCAPES:
            out.append(_PY_ESCAPES[c])
        elif c in '01234567':
            m = _PY_OCTAL.match(s, j + 1)
            out.append(chr(int(m.group(), 8)))
            i = m.end()
        elif c == 'x' and _is_hex(s[i:i + 2], 2):
            out.append(chr(int(s[i:i + 2], 16)))
            i += 2
        elif c == 'u' and _is_hex(s[i:i + 4], 4):
            out.append(chr(int(s[i:i + 4], 16)))
            i += 4
        elif c == 'U' and _is_hex(s[i:i + 8], 8) and int(s[i:i + 8], 16) <= 0x10FFFF:
            out.append(chr(int(s[i:i + 8], 16)))
            i += 8
        elif c == 'N' and s.startswith('{', i) and s.find('}', i) > 0:
            k = s.find('}', i)
            try:
                out.append(unicodedata.lookup(s[i + 1:k]))
                i = k + 1
            except KeyError:
                out.append('\\N')
        else:
            out.append('\\' + c)
    return _fix_surrogates(''.join(out))


# ------------------------------------------------------------------------------- literal value -> SQL
_SQL_BASE = r"""'|"|`|--|/\*|%%|%\([A-Za-z_][A-Za-z0-9_]*\)[sd]|%[sd]|\?\d+"""
_PY_FORMAT_FIELD = (r"""\{(?:[A-Za-z_][A-Za-z0-9_]*|\d+)?(?:\.[A-Za-z_][A-Za-z0-9_]*|\[[^\]{}'"]*\])*"""
                    r"""(?:![rsa])?(?::[^{}'"]*)?\}""")
_JS_TEXT_FIELD = r"""\$\{[^{}'"`]*\}"""
_SQL_SPECIAL = {
    '': re.compile(_SQL_BASE),
    'pyformat': re.compile(_SQL_BASE + '|' + _PY_FORMAT_FIELD),
    'jsplain': re.compile(_SQL_BASE + '|' + _JS_TEXT_FIELD),
}
_SQL_CLOSERS = {"'": "'", '"': '"', '`': '`', '--': '\n', '/*': '*/'}
_SQL_QUOTES = frozenset({"'", '"', '`'})


def _starts_like_sql(group: Sequence[_Literal]) -> bool:
    """True if the first non-blank text of the group starts with SELECT or WITH (cheap pre-check)."""
    for lit in group:
        for kind, value in lit.parts:
            if kind == _PARAM:
                return False
            if value.strip():
                return bool(_SQL_START.match(value))
    return False


@dataclass
class _SqlScan:
    """Position of the SQL text being assembled: the open SQL quote or comment, and where it began."""

    state: str = ''          # the open SQL quote or comment ('' when outside)
    start: int = 0           # index in the output list of the open quote's opening character
    dynamic: bool = False    # an interpolation occurred inside the open quote


def _group_sql(group: Sequence[_Literal]) -> str:
    """Concatenated value of a literal group with every parameter replaced by :data:`PLACEHOLDER`.

    A quoted SQL token that contains an interpolation (``'${name}'``, ``'%${q}%'``, ``"${column}"``) is one
    value known only at run time, so the whole token becomes the placeholder.
    """
    out: List[str] = []
    scan = _SqlScan()
    for lit in group:
        for kind, value in lit.parts:
            if kind == _PARAM:
                scan.dynamic = scan.dynamic or scan.state in _SQL_QUOTES
                out.append(PLACEHOLDER)
            else:
                _normalize_text(value, lit.style, scan, out)
    return ''.join(out)


def _normalize_text(s: str, style: str, scan: _SqlScan, out: List[str]) -> None:
    """Copy ``s`` to ``out``, replacing parameter syntax outside SQL quotes and comments.

    ``scan`` carries the open SQL quote or comment across literal parts and is updated. A doubled quote
    (``'it''s'``) inside a part is an escaped quote, not the end of the token.
    """
    special = _SQL_SPECIAL[style]
    i, n = 0, len(s)
    while i < n:
        if scan.state:
            closer = _SQL_CLOSERS[scan.state]
            j = s.find(closer, i)
            while scan.state in _SQL_QUOTES and j >= 0 and s.startswith(closer * 2, j):
                j = s.find(closer, j + 2)
            if j < 0:
                out.append(s[i:])
                return
            out.append(s[i:j + len(closer)])
            i = j + len(closer)
            if scan.dynamic:
                del out[scan.start:]
                out.append(PLACEHOLDER)
            scan.state, scan.dynamic = '', False
            continue
        m = special.search(s, i)
        if m is None:
            out.append(s[i:])
            return
        out.append(s[i:m.start()])
        token = m.group()
        if token in _SQL_CLOSERS:
            scan.state, scan.start, scan.dynamic = token, len(out), False
            out.append(token)
        elif token == '%%':
            out.append('%')
        else:
            out.append(PLACEHOLDER)
        i = m.end()


def _queries(sql: str, dialect: Optional[str]) -> List[str]:
    """Qualifying queries in ``sql``: [sql] for one statement, re-rendered SQL for several."""
    try:
        trees = [t for t in sqlglot.parse(sql, read=dialect) if t is not None]
    except Exception:  # noqa: BLE001 - any failure of the parser means "this text is not SQL"
        return []
    if len(trees) == 1:
        return [sql] if _qualifies(trees[0]) else []
    return [t.sql(dialect=dialect) for t in trees if _qualifies(t)]


def _qualifies(tree: exp.Expression) -> bool:
    """True for a query that reads at least one named table and has a static structure.

    The structure is dynamic when a placeholder stands for a table (``FROM ${table}``), an alias
    (``FROM orders ${where}`` parses as an alias, which means a clause was interpolated) or a column name.
    Placeholders in value positions (comparisons, function arguments, LIMIT, ...) are fine.
    """
    if not isinstance(tree, exp.Query):
        return False
    for ph in tree.find_all(exp.Placeholder):
        parent, key = ph.parent, ph.arg_key
        if (isinstance(parent, (exp.TableAlias, exp.Column)) or (isinstance(parent, exp.Table) and key == 'this')
                or (isinstance(parent, exp.Alias) and key == 'alias')):
            return False
    return any(isinstance(t.this, exp.Identifier) and t.name for t in tree.find_all(exp.Table))


# ----------------------------------------------------------------------------------- repository walk
def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Compile a glob over POSIX relative paths: '**' spans directories, '*' and '?' stay in a segment."""
    p = pattern.replace('\\', '/')
    while p.startswith('./'):
        p = p[2:]
    p = p.lstrip('/')
    out: List[str] = []
    i, n = 0, len(p)
    while i < n:
        if p.startswith('**/', i):
            out.append('(?:.*/)?')
            i += 3
        elif p.startswith('/**', i) and i + 3 == n:
            out.append('(?:/.*)?')
            i += 3
        elif p.startswith('**', i):
            out.append('.*')
            i += 2
        elif p[i] == '*':
            out.append('[^/]*')
            i += 1
        elif p[i] == '?':
            out.append('[^/]')
            i += 1
        elif p[i] == '[' and p.find(']', i + 2) > 0:
            k = p.find(']', i + 2)
            body = p[i + 1:k]
            if body.startswith('!'):
                body = '^' + body[1:]
            out.append('[' + body.replace('\\', '\\\\') + ']')
            i = k + 1
        else:
            out.append(re.escape(p[i]))
            i += 1
    return re.compile(''.join(out))


def _compile_globs(patterns: Union[str, Iterable[str]]) -> List[Tuple[re.Pattern[str], Optional[re.Pattern[str]]]]:
    """(file regex, directory-prune regex or None) per pattern; a single string is one pattern."""
    if isinstance(patterns, str):
        patterns = [patterns]
    compiled = []
    for pattern in patterns:
        p = str(pattern).replace('\\', '/')
        prune = _glob_regex(p[:-3]) if p.endswith('/**') and len(p) > 3 else None
        compiled.append((_glob_regex(p), prune))
    return compiled


def _repository_files(base: Path, include: Sequence[Tuple[re.Pattern[str], Optional[re.Pattern[str]]]],
                      exclude: Sequence[Tuple[re.Pattern[str], Optional[re.Pattern[str]]]]) -> List[Tuple[str, Path]]:
    """(relative POSIX path, path) of the files to scan, sorted by relative path."""
    found: List[Tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(base):
        rel_dir = Path(dirpath).relative_to(base).as_posix()
        prefix = '' if rel_dir == '.' else rel_dir + '/'
        dirnames[:] = sorted(d for d in dirnames
                             if not any(prune is not None and prune.fullmatch(prefix + d) for _, prune in exclude))
        for name in filenames:
            rel = prefix + name
            if any(rx.fullmatch(rel) for rx, _ in include) and not any(rx.fullmatch(rel) for rx, _ in exclude):
                found.append((rel, Path(dirpath, name)))
    found.sort(key=lambda item: item[0])
    return found


def _read_source(path: Path) -> Optional[str]:
    """File text (UTF-8, undecodable bytes replaced), or None for unreadable or binary files."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b'\x00' in data:
        return None
    return data.decode('utf-8', errors='replace')
