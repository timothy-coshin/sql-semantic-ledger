"""Table and column names written as SQL text (internal helpers for checks, engine and revalidate).

Definitions keep table names as SQL text in the database's dialect, quoted only where needed: ``orders``,
``main.orders``, ``"order items"`` (this is how :mod:`sql_semantic_ledger.parse` writes them). These helpers
take such names apart without losing quoted names, compare them, and write them back.

* Text that does not parse as a plain table name, such as ``order items`` without quotes, is taken as one
  exact name (as if it were quoted), so hand-written definitions keep working.
* Keys compare names case-insensitively, part by part (``main."Orders"`` and ``main.orders`` match).
* A part is written with quotes when it was quoted, is not a plain word, or is a keyword (``"group"``).
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, FrozenSet, Optional, Sequence, Tuple

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

#: One part of a dotted name: (the name without quotes, whether it was quoted).
Part = Tuple[str, bool]

_SAFE_IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_COMMON_RESERVED = frozenset(
    'ALL AND ANY AS ASC BETWEEN BY CASE CHECK COLUMN CONSTRAINT CREATE CROSS DEFAULT DELETE DESC DISTINCT DROP '
    'ELSE END EXCEPT EXISTS FOREIGN FROM FULL GRANT GROUP HAVING IN INDEX INNER INSERT INTERSECT INTO IS JOIN KEY '
    'LEFT LIKE LIMIT NATURAL NOT NULL OF OFFSET ON OR ORDER OUTER PRIMARY REFERENCES RIGHT ROW ROWS SELECT SET '
    'TABLE THEN TO UNION UNIQUE UPDATE USER USING VALUES WHEN WHERE WINDOW WITH'.split())


@lru_cache(maxsize=None)
def reserved_words(dialect: Any) -> FrozenSet[str]:
    """Words that must be quoted when used as identifiers (the dialect's keywords plus common SQL words)."""
    keywords = sqlglot.Dialect.get_or_raise(dialect).tokenizer_class.KEYWORDS
    return frozenset(k for k in keywords if isinstance(k, str) and k.isalpha()) | _COMMON_RESERVED


@lru_cache(maxsize=None)
def folds_case(dialect: Any) -> bool:
    """True when the dialect treats unquoted and quoted names differently (e.g. PostgreSQL, Snowflake)."""
    d = sqlglot.Dialect.get_or_raise(dialect)
    return bool(d.case_sensitive('a') or d.case_sensitive('A'))


def fold(tree: exp.Expression, dialect: Any) -> exp.Expression:
    """Normalize the identifiers of ``tree`` in place the way the database reads them.

    Unquoted names are folded to the dialect's case and quoted names are kept, so that a name taken out of
    the tree means the same object once it is written back with :func:`identifier` (``exact_case=True``).
    Dialects that ignore case (SQLite, DuckDB, ...) are left as written.
    """
    return normalize_identifiers(tree, dialect=dialect) if folds_case(dialect) else tree


def fold_name(name: str, quoted: bool, dialect: Any) -> str:
    """One name folded like :func:`fold` does (``Orders`` -> ``orders`` unquoted in PostgreSQL)."""
    if not folds_case(dialect):
        return name
    return sqlglot.Dialect.get_or_raise(dialect).normalize_identifier(exp.Identifier(this=name, quoted=quoted)).name


def identifier(name: str, dialect: Any, quoted: bool = False, exact_case: bool = False) -> exp.Identifier:
    """An identifier for ``name``, quoted when asked to, when it is not a plain word or when it is a keyword.

    With ``exact_case`` the name is taken as the exact stored name (see :func:`fold`) and is also quoted when the
    dialect would otherwise fold it to another case.
    """
    quote = (quoted or not _SAFE_IDENTIFIER.match(name) or name.upper() in reserved_words(dialect)
             or (exact_case and sqlglot.Dialect.get_or_raise(dialect).case_sensitive(name)))
    return exp.to_identifier(name, quoted=quote)


@lru_cache(maxsize=16384)
def _parsed_parts(text: str, dialect: Any) -> Optional[Tuple[Part, ...]]:
    try:
        node = sqlglot.parse_one(text, into=exp.Table, read=dialect)
    except Exception:  # noqa: BLE001 - sqlglot raises several error types; the text is then a raw name
        return None
    if not isinstance(node, exp.Table) or any(v for k, v in node.args.items() if k not in ('catalog', 'db', 'this')):
        return None
    parts = node.parts
    if not parts or not all(isinstance(p, exp.Identifier) and p.name for p in parts):
        return None
    return tuple((p.name, bool(p.args.get('quoted'))) for p in parts)


def table_parts(text: str, dialect: Any = None) -> Tuple[Part, ...]:
    """The (name, quoted) parts of a table name written as SQL text.

    ``main."Order Items"`` -> (('main', False), ('Order Items', True)). Text that is not a plain table name is
    one exact part, as if quoted: ``order items`` -> (('order items', True),).
    """
    stripped = text.strip()
    if not stripped:
        return ()
    return _parsed_parts(stripped, dialect) or ((stripped, True),)


def table_key(text: str, dialect: Any = None, schema: bool = True) -> str:
    """Case-insensitive key of a table name written as SQL text (without schema when ``schema`` is False)."""
    names = [name.lower() for name, _ in table_parts(text, dialect)]
    return '.'.join(names if schema else names[-1:])


def parts_table(parts: Sequence[Part], dialect: Any, exact_case: bool = False) -> exp.Table:
    """A table expression for the given parts (catalog, db, table; extra leading parts are joined by dots)."""
    idents = [identifier(name, dialect, quoted=quoted, exact_case=exact_case) for name, quoted in parts]
    if not idents:
        raise ValueError('a table name needs at least one part')
    if len(idents) > 3:
        idents = [exp.Dot.build(idents[:-2])] + idents[-2:]
    keys = ('catalog', 'db', 'this')[-len(idents):]
    return exp.Table(**dict(zip(keys, idents)))


def table_sql(text: str, dialect: Any = None) -> str:
    """A table name written as SQL text again, quoted where needed (``order items`` -> ``"order items"``)."""
    return parts_table(table_parts(text, dialect), dialect).sql(dialect=dialect)


def column_table_parts(column: exp.Column) -> Tuple[Part, ...]:
    """The parts of the table that qualifies ``column`` (empty when the column is unqualified)."""
    out = []
    for key in ('catalog', 'db', 'table'):
        part = column.args.get(key)
        if isinstance(part, exp.Identifier) and part.name:
            out.append((part.name, bool(part.args.get('quoted'))))
        elif part is not None and getattr(part, 'name', ''):
            out.append((part.name, False))
    return tuple(out)
