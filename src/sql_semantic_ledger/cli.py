"""Command-line tool ``ledger``.

CONTRACT (implemented by the build step), argparse sub-commands:
    ledger features [--lang en]
    ledger induce HISTORY [--dialect D] [--min-support 2] [--code-root DIR] [--no-metrics] -o LEDGER.json
    ledger scan-code ROOT [-o queries.jsonl]
    ledger check LEDGER.json --db DB.sqlite
    ledger certify LEDGER.json ID --by NAME [--reject] [--note TEXT]
    ledger show LEDGER.json [--min-grade E1] [--kind metric]
    ledger decide LEDGER.json ID [ID ...] [--eps E0=1,E1=0.6,E2=0.3,E3=0.05,E4=0.02] [--delta 0.02] [--target 0.05]
    ledger plan LEDGER.json --answers answers.jsonl [--changed ID ...] [--changed-table T ...] [--budget N] [--eps ...]
main(argv=None) -> int returns a process exit code.

Behaviour
---------
* Exit codes: 0 success; 1 the command ran and found a problem (``check``: at least one check failed, so
  definitions were falsified); 2 an error (bad arguments, a missing or invalid file, an unknown definition,
  a planned feature, ...). An error is one line ``ledger: error: ...`` on stderr; ``-v`` adds the traceback
  of unexpected errors. ``-v`` also shows INFO log messages (for now the ``induce`` summary) and ``-vv`` debug
  details on stderr.
* Definitions are named by id or, when the name is unique, by name (case-insensitive).
* ``features`` prints :func:`features.table` (``--lang en`` by default, or ``ko``).
* ``induce`` reads HISTORY with :func:`induce.load_history` (``.sql`` or ``.jsonl``, so the output of
  ``scan-code`` can be read back), adds the SQL found under ``--code-root``, and writes LEDGER.json. When the
  file exists, the new evidence is merged into it (the ledger is append-only); otherwise it is created.
* ``scan-code`` writes one JSON object per query, ``{"sql", "id", "kind": "code", "ref"}``, to ``-o`` or to
  stdout (the summary then goes to stderr). ``--dialect`` is passed to the parse check.
* ``check`` opens the SQLite file read-only and records every check as evidence. It refuses to run, and records
  nothing, when the file is not a SQLite database or holds none of the ledger's tables (a wrong ``--db`` would
  otherwise falsify every definition).
* ``check``, ``certify`` and ``induce`` write the ledger back atomically (temporary file, then rename).
* ``--eps`` is a comma-separated list ``GRADE=RATE`` (``E0=1,E1=0.6``). ``decide`` computes the error bound
  only when ``--eps`` is given (a grade missing from it counts as 1.0, so the bound stays valid); ``--delta``
  and ``--target`` take effect only together with ``--eps``. ``plan`` uses the default risks of
  :data:`revalidate.DEFAULT_RISKS` for the grades ``--eps`` leaves out.
* ``show``, ``check``, ``decide`` and ``plan`` accept ``--json`` for machine-readable output. JSON output is
  UTF-8 with ``ensure_ascii=False`` whatever the console encoding; the ledger file is written the same way.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import traceback
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, TextIO

from . import __version__
from ._unimplemented import NotImplementedYet
from .model import SCHEMA_VERSION, Definition, Grade, Kind, Ledger

EXIT_OK = 0
EXIT_PROBLEM = 1
EXIT_ERROR = 2

_PACKAGE_LOGGER = logging.getLogger('sql_semantic_ledger')
_KIND_ORDER = {Kind.METRIC: 0, Kind.MEASURE: 1, Kind.FILTER: 2, Kind.JOIN: 3, Kind.DIMENSION: 4}


class CLIError(Exception):
    """A problem with the user's input (bad argument, missing or invalid file); printed as one line, exit 2."""


# ---------------------------------------------------------------------------------------------- entry point
def main(argv: Optional[List[str]] = None) -> int:
    """Run the ``ledger`` command line with ``argv`` (default: ``sys.argv[1:]``) and return the exit code."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as stop:          # argparse exits after --help/--version (0) and on usage errors (2)
        return _exit_status(stop.code)
    verbosity = getattr(args, 'verbose', 0) or 0
    with _logging_to_stderr(verbosity):
        try:
            return args.handler(args)
        except CLIError as error:
            return _fail(str(error))
        except NotImplementedYet as error:
            return _fail(str(error))
        except KeyboardInterrupt:
            return 130
        except BrokenPipeError:         # e.g. `ledger scan-code . | head`
            _silence_stdout()
            return EXIT_OK
        except Exception as error:      # noqa: BLE001 - every failure becomes a one-line message and exit 2
            return _fail(_describe_error(error), with_traceback=verbosity > 0)


def build_parser() -> argparse.ArgumentParser:
    """The argparse parser of ``ledger`` with one sub-parser per command."""
    parser = argparse.ArgumentParser(
        prog='ledger',
        description='LEDGER: business definitions induced from SQL usage and service code, graded by evidence '
                    '(E0-E4) and used to confirm, warn about or reject answers; the stored answers a change '
                    'affects are ranked for revalidation.',
        epilog='Exit codes: 0 success, 1 a check failed (check), 2 error.')
    parser.add_argument('--version', action='version', version=f'%(prog)s (sql-semantic-ledger) {__version__}')
    parser.add_argument('-v', '--verbose', action='count', default=0,
                        help='tracebacks of unexpected errors and log messages on stderr (-vv: debug details)')
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('-v', '--verbose', action='count', default=argparse.SUPPRESS,
                        help='tracebacks of unexpected errors and log messages on stderr (-vv: debug details)')
    commands = parser.add_subparsers(dest='command', metavar='COMMAND', required=True)

    p = commands.add_parser('features', parents=[common], help='list implemented, partial and planned features',
                            description='Print the feature table (implemented / partial / planned).')
    p.add_argument('--lang', choices=('en', 'ko'), default='en', help='table language (default: en)')
    p.set_defaults(handler=_cmd_features)

    p = commands.add_parser('induce', parents=[common], help='induce definitions from a usage history (and code)',
                            description='Induce MEASURE, FILTER, JOIN and METRIC definitions from a SQL usage '
                                        'history and, optionally, from SQL found in service code.')
    p.add_argument('history', metavar='HISTORY', help="usage history: .sql (statements separated by ';') or .jsonl")
    p.add_argument('--dialect', type=_dialect_arg, default=None,
                   help='sqlglot dialect of the SQL, also used to split a .sql history (default: sqlglot generic SQL)')
    p.add_argument('--min-support', type=_positive_int, default=2, metavar='N',
                   help='independent queries or code sites a new definition needs (default: 2, which gives E1)')
    p.add_argument('--code-root', metavar='DIR', default=None, help='also scan SQL strings in the code under DIR')
    p.add_argument('--no-metrics', action='store_true', help='do not create METRIC definitions')
    p.add_argument('-o', '--output', required=True, metavar='LEDGER.json',
                   help='ledger file to write (merged into when it exists)')
    p.set_defaults(handler=_cmd_induce)

    p = commands.add_parser('scan-code', parents=[common], help='find SQL strings in JS/TS/Python code',
                            description='Find SQL queries in the string literals of JS/TS/Python files and write '
                                        'them as JSON lines that `ledger induce` can read.')
    p.add_argument('root', metavar='ROOT', help='directory (or single file) to scan')
    p.add_argument('-o', '--output', metavar='queries.jsonl', default=None, help='write here instead of stdout')
    p.add_argument('--dialect', type=_dialect_arg, default=None, help='sqlglot dialect for the parse check')
    p.set_defaults(handler=_cmd_scan_code)

    p = commands.add_parser('check', parents=[common], help='run data checks against a SQLite database (E2)',
                            description='Run the data checks of every definition against a SQLite database and '
                                        'record them as evidence. Exit code 1 when a check fails.')
    p.add_argument('ledger', metavar='LEDGER.json')
    p.add_argument('--db', required=True, metavar='DB.sqlite', help='SQLite database file (opened read-only)')
    p.add_argument('--json', action='store_true', help='print the results as JSON')
    p.set_defaults(handler=_cmd_check)

    p = commands.add_parser('certify', parents=[common], help="record a person's certification (E3) or rejection (E0)",
                            description="Record a person's decision about a definition.")
    p.add_argument('ledger', metavar='LEDGER.json')
    p.add_argument('id', metavar='ID', help='definition id (or unique name)')
    p.add_argument('--by', required=True, metavar='NAME', help='who certifies')
    p.add_argument('--reject', action='store_true', help='record a rejection instead of a certification')
    p.add_argument('--note', default='', metavar='TEXT', help='why (stored with the decision)')
    p.set_defaults(handler=_cmd_certify)

    p = commands.add_parser('show', parents=[common], help='list definitions with grades and evidence',
                            description='List the definitions of a ledger, strongest evidence first.')
    p.add_argument('ledger', metavar='LEDGER.json')
    p.add_argument('--min-grade', type=_grade_arg, default=None, metavar='E1', help='only grades >= this (E0-E4)')
    p.add_argument('--kind', choices=[k.value for k in Kind], default=None, help='only this kind')
    p.add_argument('--json', action='store_true', help='print the definitions as JSON')
    p.set_defaults(handler=_cmd_show)

    p = commands.add_parser('decide', parents=[common], help='confirm / warn / reject an answer (with error bound)',
                            description='Decide an answer that uses the given definitions: confirm (all E3+), '
                                        'warn (machine evidence only) or reject (E0, falsified or expired).')
    p.add_argument('ledger', metavar='LEDGER.json')
    p.add_argument('ids', nargs='+', metavar='ID', help='definitions the answer uses (ids or unique names)')
    p.add_argument('--eps', type=_eps_arg, default=None, metavar='E0=1,E1=0.6,...',
                   help='per-grade error rates; enables the error bound')
    p.add_argument('--delta', type=_probability_arg, default=0.0, metavar='P',
                   help='error of choosing or assembling the definitions (default: 0)')
    p.add_argument('--target', type=_probability_arg, default=None, metavar='P',
                   help='a confirmed answer needs error bound <= P, else it is downgraded to warn')
    p.add_argument('--json', action='store_true', help='print the decision as JSON')
    p.set_defaults(handler=_cmd_decide)

    p = commands.add_parser('plan', parents=[common], help='rank stored answers for revalidation after changes',
                            description='Rank the stored answers affected by changed definitions or tables by '
                                        'weight x probability that a definition they use is wrong.')
    p.add_argument('ledger', metavar='LEDGER.json')
    p.add_argument('--answers', required=True, metavar='answers.jsonl',
                   help='stored answers, one JSON object per line: {"id", "definitions", "tables", "weight"}')
    p.add_argument('--changed', nargs='+', action='extend', default=[], metavar='ID',
                   help='changed definitions (ids or unique names)')
    p.add_argument('--changed-table', nargs='+', action='extend', default=[], metavar='T', help='changed tables')
    p.add_argument('--budget', type=_non_negative_int, default=None, metavar='N', help='answers to revalidate')
    p.add_argument('--eps', type=_eps_arg, default=None, metavar='E0=1,E1=0.5,...',
                   help='per-grade error rates (default: E0 1, E1 0.5, E2 0.3, E3 0.1, E4 0.05)')
    p.add_argument('--json', action='store_true', help='print the plan as JSON')
    p.set_defaults(handler=_cmd_plan)
    return parser


# ---------------------------------------------------------------------------------------------- commands
def _cmd_features(args: argparse.Namespace) -> int:
    from .features import table

    _say(table(args.lang))
    return EXIT_OK


def _cmd_induce(args: argparse.Namespace) -> int:
    from .induce import induce, load_history

    history_path = _existing_file(args.history, 'history file')
    records = load_history(history_path, dialect=args.dialect)
    code_records: List[Any] = []
    if args.code_root is not None:
        from .code_sources import scan_repository

        code_records = scan_repository(_existing_path(args.code_root, 'code root'), dialect=args.dialect)
    output = Path(args.output)
    existing = _load_ledger(output) if output.exists() else None
    before = len(existing) if existing is not None else 0
    ledger = induce(list(records) + code_records, dialect=args.dialect, min_support=args.min_support,
                    ledger=existing, metrics=not args.no_metrics)
    _save_ledger(ledger, output)
    source = f'read {len(records)} {"query" if len(records) == 1 else "queries"} from {history_path}'
    source += f' ({_kinds_text(records)})' if records else ''
    if args.code_root is not None:
        source += f'; found {_records_text(code_records)} under {args.code_root}'
    _say(source)
    verb = f'updated {output} (+{len(ledger) - before} new)' if existing is not None else f'wrote {output}'
    _say(f'{verb}: {len(ledger)} definitions ({_grade_counts(ledger)})')
    return EXIT_OK


def _cmd_scan_code(args: argparse.Namespace) -> int:
    from .code_sources import scan_repository

    records = scan_repository(_existing_path(args.root, 'code root'), dialect=args.dialect)
    lines = ''.join(json.dumps({'sql': r.sql, 'id': r.id, 'kind': r.kind.value, 'ref': r.ref},
                               ensure_ascii=False) + '\n' for r in records)
    summary = f'found {_records_text(records)} under {args.root}'
    if args.output is None:
        _write_utf8(lines)
        _say(summary, sys.stderr)
    else:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(lines, encoding='utf-8')
        _say(f'{summary}; wrote {output}')
    return EXIT_OK


def _cmd_check(args: argparse.Namespace) -> int:
    from .checks import run_checks

    path = Path(args.ledger)
    ledger = _load_ledger(path)
    if len(ledger) == 0:
        if args.json:
            _emit_json({'checked': 0, 'passed': 0, 'failed': 0, 'results': [], 'grades': {}})
        else:
            _say(f'{path} has no definitions to check')
        return EXIT_OK
    with closing(_connect_readonly(args.db)) as conn:
        _require_known_tables(ledger, conn, args.db)
        results = run_checks(ledger, conn)
    _save_ledger(ledger, path)
    failed = [r for r in results if not r.passed]
    if args.json:
        _emit_json({'checked': len({r.definition_id for r in results}), 'passed': len(results) - len(failed),
                    'failed': len(failed),
                    'results': [{'definition_id': r.definition_id, 'name': ledger.get(r.definition_id).name,
                                 'check': r.name, 'passed': r.passed, 'detail': r.detail} for r in results],
                    'grades': {d.id: d.grade.name for d in ledger}})
    else:
        for r in results:
            _say(f"{'PASS' if r.passed else 'FAIL'}  {r.name:<15} {_label(ledger.get(r.definition_id))}: {r.detail}")
        checked = len({r.definition_id for r in results})
        _say(f'checked {checked} definitions against {args.db}: {len(results) - len(failed)} of {len(results)} '
             f'checks passed')
        _say(f'grades now: {_grade_counts(ledger)}')
        if failed:
            names = sorted({ledger.get(r.definition_id).name for r in failed})
            _say(f'falsified: {", ".join(names)}')
    return EXIT_PROBLEM if failed else EXIT_OK


def _cmd_certify(args: argparse.Namespace) -> int:
    path = Path(args.ledger)
    ledger = _load_ledger(path)
    definition = _resolve(ledger, args.id)
    by = args.by.strip()
    if not by:
        raise CLIError('--by needs the name of the person who decides')
    before = definition.grade
    ledger.certify(definition.id, by=by, note=args.note, passed=not args.reject)
    _save_ledger(ledger, path)
    action = 'recorded rejection of' if args.reject else 'certified'
    _say(f'{action} {_label(definition)} by {by}: {before.name} -> {definition.grade.name}')
    return EXIT_OK


def _cmd_show(args: argparse.Namespace) -> int:
    path = Path(args.ledger)
    ledger = _load_ledger(path)
    definitions = sorted(ledger.find(kind=args.kind, min_grade=args.min_grade),
                         key=lambda d: (-int(d.grade), _KIND_ORDER.get(d.kind, 9), d.name, d.id))
    if args.json:
        _emit_json([d.to_dict() for d in definitions])
        return EXIT_OK
    filters = [text for text in (f'kind {args.kind}' if args.kind else '',
                                 f'grade >= {args.min_grade.name}' if args.min_grade is not None else '') if text]
    shown = f'{len(definitions)} of {len(ledger)} definitions' if filters else f'{len(ledger)} definitions'
    _say(f'{shown} in {path} ({_grade_counts(definitions)})' + (f'; {", ".join(filters)}' if filters else ''))
    for d in definitions:
        _say(f'{d.grade.name}  {d.kind.value:<7}  {d.id}  {d.name}  {_status_text(d)}')
        _say(f'      {_sql_text(d)}')
    return EXIT_OK


def _cmd_decide(args: argparse.Namespace) -> int:
    from .grade import Policy, decide

    ledger = _load_ledger(Path(args.ledger))
    definitions = _unique([_resolve(ledger, key) for key in args.ids])
    if args.eps is None and (args.target is not None or args.delta):
        _say('note: --delta and --target take effect only with --eps', sys.stderr)
    decision = decide(definitions, Policy(target=args.target), eps=args.eps, delta=args.delta)
    if args.json:
        _emit_json({'verdict': decision.verdict.value, 'bound': decision.bound, 'reasons': decision.reasons,
                    'grades': decision.grades, 'definitions': [d.id for d in definitions]})
        return EXIT_OK
    bound = '' if decision.bound is None else f' (error bound {decision.bound:.4g})'
    _say(f'{decision.verdict.value.upper()}{bound}')
    for reason in decision.reasons:
        _say(f'- {reason}')
    return EXIT_OK


def _cmd_plan(args: argparse.Namespace) -> int:
    from .revalidate import DependencyIndex, plan

    ledger = _load_ledger(Path(args.ledger))
    answers = _load_answers(_existing_file(args.answers, 'answers file'))
    changed = [_resolve(ledger, key).id for key in args.changed]
    items = plan(ledger, DependencyIndex(answers), changed_definitions=list(dict.fromkeys(changed)),
                 changed_tables=list(dict.fromkeys(args.changed_table)), eps=args.eps)
    selected = items if args.budget is None else items[:args.budget]
    if args.json:
        _emit_json({'candidates': len(items), 'budget': args.budget,
                    'items': [{'answer_id': i.answer_id, 'score': i.score, 'reason': i.reason} for i in selected]})
        return EXIT_OK
    if not items:
        _say(f'no stored answer (of {len(answers)}) is affected by the changes')
        return EXIT_OK
    scope = 'changed definitions or tables' if changed or args.changed_table else 'no change given: all answers'
    budget = f', budget {args.budget}' if args.budget is not None else ''
    _say(f'revalidation plan: {len(selected)} of {len(items)} candidate answers ({scope}{budget})')
    width = len(str(len(selected)))
    for rank, item in enumerate(selected, start=1):
        _say(f'{rank:>{width}}. {item.answer_id}  score {item.score:.3f}  {item.reason}')
    return EXIT_OK


# ---------------------------------------------------------------------------------------------- argument types
def _positive_int(text: str) -> int:
    value = _integer(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f'must be an integer >= 1, got {text!r}')
    return value


def _non_negative_int(text: str) -> int:
    value = _integer(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f'must be an integer >= 0, got {text!r}')
    return value


def _integer(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f'not an integer: {text!r}') from None


def _probability_arg(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f'not a number: {text!r}') from None
    if not 0.0 <= value <= 1.0:
        raise argparse.ArgumentTypeError(f'must be a probability in [0, 1], got {text!r}')
    return value


def _dialect_arg(text: str) -> str:
    """A sqlglot dialect name, checked before anything is read."""
    import sqlglot

    try:
        sqlglot.Dialect.get_or_raise(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None
    return text


def _grade_arg(text: str) -> Grade:
    try:
        return Grade.parse(text)
    except (KeyError, ValueError, TypeError):
        raise argparse.ArgumentTypeError(f'not an evidence grade E0-E4: {text!r}') from None


def _eps_arg(text: str) -> Dict[str, float]:
    """``'E0=1,E1=0.6'`` -> ``{'E0': 1.0, 'E1': 0.6}``, validated by :func:`bound.normalize_eps`."""
    from .bound import normalize_eps

    rates: Dict[str, float] = {}
    for part in (p.strip() for p in text.split(',')):
        if not part:
            continue
        grade, sep, value = part.partition('=')
        grade = grade.strip()
        if not sep or not grade or not value.strip():
            raise argparse.ArgumentTypeError(f'expected GRADE=RATE pairs such as E1=0.6, got {part!r}')
        if grade in rates:
            raise argparse.ArgumentTypeError(f'grade {grade} is given more than once')
        try:
            rates[grade] = float(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f'not a number in {part!r}') from None
    if not rates:
        raise argparse.ArgumentTypeError('expected GRADE=RATE pairs such as E0=1,E1=0.6')
    try:
        return {grade.name: rate for grade, rate in normalize_eps(rates).items()}
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


# ---------------------------------------------------------------------------------------------- files
def _existing_path(text: str, what: str) -> Path:
    path = Path(text)
    if not path.exists():
        raise CLIError(f'{what} not found: {text}')
    return path


def _existing_file(text: str, what: str) -> Path:
    path = _existing_path(text, what)
    if not path.is_file():
        raise CLIError(f'{what} is not a file: {text}')
    return path


def _load_ledger(path: Path) -> Ledger:
    """Read a ledger file, turning every kind of bad file into a CLIError."""
    if not path.exists():
        raise CLIError(f'ledger file not found: {path} (create one with: ledger induce HISTORY -o {path})')
    if not path.is_file():
        raise CLIError(f'ledger path is not a file: {path}')
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except UnicodeDecodeError:
        raise CLIError(f'{path} is not a ledger file (not UTF-8 text)') from None
    except json.JSONDecodeError as error:
        raise CLIError(f'{path} is not a ledger file: invalid JSON ({error.msg}, line {error.lineno})') from None
    if not isinstance(data, dict) or not isinstance(data.get('definitions', []), list):
        raise CLIError(f'{path} is not a ledger file: expected a JSON object with a "definitions" list')
    version = data.get('schema_version', SCHEMA_VERSION)
    if not isinstance(version, int) or version > SCHEMA_VERSION:
        raise CLIError(f'{path} has ledger schema version {version!r}; this release reads up to {SCHEMA_VERSION}')
    try:
        return Ledger.from_dict(data)
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise CLIError(f'{path} is not a valid ledger file: {_describe_error(error)}') from None


def _save_ledger(ledger: Ledger, path: Path) -> None:
    """Write the ledger atomically: to a temporary file next to ``path``, then rename it over ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    try:
        ledger.save(temporary)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_answers(path: Path) -> List[Any]:
    """Stored answers from a jsonl file (one AnswerRecord object per line; blank lines are skipped)."""
    from .revalidate import AnswerRecord

    answers: List[Any] = []
    with path.open(encoding='utf-8-sig') as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            where = f'{path}:{line_no}'
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as error:
                raise CLIError(f'{where}: invalid JSON ({error.msg})') from None
            if not isinstance(obj, dict):
                raise CLIError(f'{where}: expected a JSON object such as {{"id": "a1", "definitions": [...]}}')
            try:
                answers.append(AnswerRecord.from_dict(obj))
            except (TypeError, ValueError) as error:
                raise CLIError(f'{where}: {error}') from None
    return answers


def _connect_readonly(db: str) -> sqlite3.Connection:
    """Open a SQLite file read-only; CLIError if it is missing or not a SQLite database."""
    path = Path(db)
    if not path.is_file():
        raise CLIError(f'database file not found: {db}')
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    except sqlite3.Error as error:
        raise CLIError(f'cannot open {db}: {error}') from None
    try:
        conn.execute('SELECT name FROM sqlite_master LIMIT 1').fetchall()
    except sqlite3.Error as error:
        conn.close()
        raise CLIError(f'{db} is not a usable SQLite database: {error}') from None
    return conn


def _require_known_tables(ledger: Ledger, conn: sqlite3.Connection, db: str) -> None:
    """CLIError when none of the tables the ledger refers to exists in the database (wrong ``--db``)."""
    wanted = {_bare_table(t) for d in ledger for t in d.tables if t and t.strip()}
    if not wanted:
        return
    present = {str(row[0]).lower() for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
    if not wanted & present:
        shown = ', '.join(sorted(wanted)[:5]) + (', ...' if len(wanted) > 5 else '')
        raise CLIError(f'none of the ledger tables ({shown}) exists in {db}; is it the right database? '
                       f'Nothing was recorded.')


def _bare_table(name: str) -> str:
    """'main."Order Items"' -> 'order items': the table name without schema and quotes, lower case."""
    from sqlglot import exp

    try:
        return exp.to_table(name.strip(), dialect='sqlite').name.lower()
    except Exception:  # noqa: BLE001 - fall back to plain text handling
        return name.strip().rsplit('.', 1)[-1].strip('"`[]').lower()


# ---------------------------------------------------------------------------------------------- definitions
def _resolve(ledger: Ledger, key: str) -> Definition:
    """The definition with id ``key``, or the only one named ``key`` (case-insensitive)."""
    text = key.strip()
    if text in ledger:
        return ledger.get(text)
    named = sorted(ledger.find(name=text), key=lambda d: d.id)
    if len(named) == 1:
        return named[0]
    if named:
        raise CLIError(f'{len(named)} definitions are named {text!r}; use an id: ' + ', '.join(d.id for d in named))
    raise CLIError(f'no definition with id or name {text!r}')


def _unique(definitions: Iterable[Definition]) -> List[Definition]:
    out: Dict[str, Definition] = {}
    for d in definitions:
        out.setdefault(d.id, d)
    return list(out.values())


def _label(d: Definition) -> str:
    return f'{d.name} [{d.id}]'


def _sql_text(d: Definition) -> str:
    """'<expression> WHERE c1 AND c2'; an OR condition among several is shown in parentheses."""
    if not d.conditions:
        return d.expression
    shown = d.conditions if len(d.conditions) == 1 else [_grouped(c) for c in d.conditions]
    return f'{d.expression} WHERE ' + ' AND '.join(shown)


def _grouped(condition: str) -> str:
    """``condition`` in parentheses when it is an OR group (``a = 1 OR b = 2``), else unchanged."""
    import sqlglot
    from sqlglot import exp

    try:
        node = sqlglot.parse_one(condition)
    except Exception:  # noqa: BLE001 - display only: show the text as stored
        return condition
    groups = tuple(t for t in (getattr(exp, 'Or', None), getattr(exp, 'Xor', None)) if t is not None)
    return f'({condition})' if isinstance(node, groups) else condition


def _status_text(d: Definition) -> str:
    parts = [f'support {d.support}' + (f' ({", ".join(d.sources)})' if d.sources else '')]
    if d.version > 1:
        parts.append(f'version {d.version}')
    if d.falsified:
        parts.append('falsified')
    if not d.is_valid():
        parts.append('not valid now')
    return ', '.join(parts)


def _grade_counts(definitions: Iterable[Definition]) -> str:
    counts = {g: 0 for g in Grade}
    for d in definitions:
        counts[d.grade] += 1
    return ', '.join(f'{g.name} {n}' for g, n in counts.items())


def _kinds_text(records: Sequence[Any]) -> str:
    """'12 history, 5 code': how many records of each usage kind."""
    counts: Dict[str, int] = {}
    for r in records:
        kind = getattr(r.kind, 'value', str(r.kind))
        counts[kind] = counts.get(kind, 0) + 1
    return ', '.join(f'{n} {kind}' for kind, n in sorted(counts.items(), key=lambda kv: (kv[0] != 'history', kv[0])))


def _records_text(records: Sequence[Any]) -> str:
    files = {str(r.ref).rsplit(':', 1)[0] for r in records if r.ref}
    return f'{len(records)} SQL {"query" if len(records) == 1 else "queries"} in {len(files)} ' \
           f'{"file" if len(files) == 1 else "files"}'


# ---------------------------------------------------------------------------------------------- output
def _say(text: str = '', stream: Optional[TextIO] = None) -> None:
    """Write one line of text; characters the stream cannot encode are replaced instead of failing."""
    out = stream if stream is not None else sys.stdout
    line = text + '\n'
    try:
        out.write(line)
    except UnicodeEncodeError:
        encoding = getattr(out, 'encoding', None) or 'ascii'
        out.write(line.encode(encoding, 'replace').decode(encoding, 'replace'))


def _write_utf8(text: str) -> None:
    """Write ``text`` to stdout as UTF-8 bytes, whatever the console encoding (used for JSON output)."""
    out = sys.stdout
    encoding = (getattr(out, 'encoding', None) or '').lower().replace('-', '').replace('_', '')
    buffer = getattr(out, 'buffer', None)
    if encoding == 'utf8' or buffer is None:
        out.write(text)
        return
    out.flush()
    buffer.write(text.encode('utf-8'))
    buffer.flush()


def _emit_json(obj: Any) -> None:
    _write_utf8(json.dumps(obj, ensure_ascii=False, indent=2) + '\n')


def _fail(message: str, with_traceback: bool = False) -> int:
    if with_traceback:
        traceback.print_exc(file=sys.stderr)
    _say(f'ledger: error: {message}', sys.stderr)
    return EXIT_ERROR


def _describe_error(error: BaseException) -> str:
    """A short, readable message for an exception raised by the library."""
    if isinstance(error, OSError) and getattr(error, 'filename', None):
        return f'{error.strerror or error}: {error.filename}'
    if isinstance(error, KeyError):
        return str(error.args[0]) if error.args else 'unknown key'
    if isinstance(error, sqlite3.Error):
        return f'database error: {error}'
    if isinstance(error, (ValueError, TypeError, OSError, ImportError, RuntimeError)):
        return str(error) or type(error).__name__
    return f'{type(error).__name__}: {error}'


def _exit_status(code: Any) -> int:
    """argparse's SystemExit code as an int (a message string means failure)."""
    if code is None:
        return EXIT_OK
    if isinstance(code, int):
        return code
    _say(str(code), sys.stderr)
    return EXIT_ERROR


def _silence_stdout() -> None:
    """After a broken pipe, point stdout at devnull so that the interpreter's final flush stays quiet."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        pass


@contextmanager
def _logging_to_stderr(verbosity: int) -> Iterator[None]:
    """Log the package's INFO (-v) or DEBUG (-vv) messages to stderr while a command runs."""
    if verbosity <= 0:
        yield
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter('%(levelname)s %(name)s: %(message)s'))
    old_level = _PACKAGE_LOGGER.level
    _PACKAGE_LOGGER.addHandler(handler)
    _PACKAGE_LOGGER.setLevel(logging.INFO if verbosity == 1 else logging.DEBUG)
    try:
        yield
    finally:
        _PACKAGE_LOGGER.removeHandler(handler)
        _PACKAGE_LOGGER.setLevel(old_level)


if __name__ == '__main__':
    raise SystemExit(main())
