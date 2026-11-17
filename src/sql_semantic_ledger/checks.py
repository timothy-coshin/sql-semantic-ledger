"""Data checks (E2) and counterexample checks (E4) against a live database (DB-API 2.0 connection).

CONTRACT (implemented by the build step):

    run_checks(ledger, conn, definition_ids=None, dialect='sqlite') -> list[CheckResult]
        For each definition (all, or the given ids) run the checks below, add one
        Evidence(kind=DATA_CHECK, ref=<check name>, passed=..., detail=...) per check to the ledger,
        and return the results. A failed check falsifies the current version (grade E0).
          * 'columns_exist'  every referenced table and column exists
          * MEASURE/METRIC  'executes'  SELECT <expression> FROM <tables> [JOIN ...] [WHERE <conditions>] runs
                            and returns a non-NULL value, computed from at least one row (so a COUNT whose
                            conditions match no row fails like the FILTER with those conditions)
          * FILTER          'selects_rows'  the predicate selects at least one row
          * JOIN            'joins_rows'  the join returns rows, and 'key_cardinality': at least one side is unique
        Multi-table definitions use JOIN definitions from the ledger to connect their tables; if none
        connects them, the 'executes' check fails with a clear detail.
    counterexample_checks(ledger, conn, definition_id, violations) -> list[CheckResult]
        Partial E4: each violation is a SQL predicate over the definition's tables describing rows that must
        not exist if the certified definition holds; passes when SELECT COUNT(*) ... WHERE <violation> = 0.
        Adds Evidence(kind=COUNTEREXAMPLE). User-reported counterexamples are planned.

How the checks run:
    * Referenced tables and columns are the definition's ``tables`` and ``columns`` plus the columns named in
      its expression and conditions. Columns inside a subquery belong to that subquery and are left to the SQL
      checks; a table used only inside subqueries is checked for existence but not added to FROM.
    * ``tables`` and ``columns`` are SQL text in the connection's dialect, quoted where needed (``"order items"``,
      ``frpm."free meal count (k-12)"``), as :mod:`sql_semantic_ledger.induce` writes them; text that is not a
      plain name (``order items`` without quotes) is taken as one raw name. In dialects where case matters
      (PostgreSQL, Snowflake, ...) unquoted names are folded as the database folds them and quoted names keep
      their case.
    * Columns are looked up with ``PRAGMA table_info`` for SQLite, otherwise with a ``SELECT * FROM t LIMIT 0``
      probe (rendered for the dialect by sqlglot) and ``cursor.description``. Names compare case-insensitively,
      and tables are identified by their name without schema.
    * SQL fragments are embedded in the check SQL unchanged, so they must be written in the connection's dialect.
      A fragment holding a statement separator ';', unbalanced parentheses or a whole statement (DELETE, DROP,
      PRAGMA, ...) is refused: its checks fail without running it. A line comment inside a fragment is closed with
      a newline so that it cannot swallow the rest of the check query. Checks only read, but use a read-only
      connection anyway.
    * Within one run, JOIN definitions are checked first. The other definitions are then connected only through
      JOIN definitions that are neither falsified nor expired: the shortest chain, preferring higher grade and
      support. Results are returned in input order, with the checks of each definition in the order above.
    * A statement the database rejects is a failed check whose detail holds the error. On engines whose
      transactions abort on an error (e.g. PostgreSQL) ``conn.rollback()`` is then called, so use a connection
      without uncommitted changes. A connection that cannot run ``SELECT 1`` raises RuntimeError and nothing is
      recorded.
    * counterexample_checks takes the dialect from the connection's driver (SQLite, PostgreSQL, MySQL, DuckDB, ...;
      generic SQL otherwise). It raises ValueError and records nothing when a violation cannot be evaluated,
      because a broken check is not evidence against the definition. FILTER definitions are not applied
      automatically: write the full predicate of the rows that must not exist.
"""
from __future__ import annotations

import sqlite3
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.tokens import TokenType

from . import _names
from .model import Definition, Evidence, EvidenceKind, Kind, Ledger


@dataclass
class CheckResult:
    definition_id: str
    name: str
    passed: bool
    detail: str = ''


# ---------------------------------------------------------------------------------------------- identifiers
_SQLITE_ROWID = frozenset({'rowid', 'oid', '_rowid_'})
_DUPLICATES_ALIAS = 'ledger_dup'
_LEFT_ALIAS, _RIGHT_ALIAS = 'ledger_l', 'ledger_r'
_DRIVER_DIALECTS = (('sqlite3', 'sqlite'), ('psycopg', 'postgres'), ('pg8000', 'postgres'), ('pymysql', 'mysql'),
                    ('MySQLdb', 'mysql'), ('mysql', 'mysql'), ('duckdb', 'duckdb'), ('snowflake', 'snowflake'),
                    ('oracledb', 'oracle'), ('cx_Oracle', 'oracle'), ('pymssql', 'tsql'))
_STATEMENTS = tuple(getattr(exp, name) for name in (
    'Insert', 'Update', 'Delete', 'Merge', 'Create', 'Drop', 'Alter', 'Command', 'Transaction', 'Commit', 'Rollback',
    'Set', 'Use', 'Pragma', 'Copy', 'Grant', 'Revoke', 'Attach', 'Detach', 'Cache', 'Uncache', 'LoadData',
    'TruncateTable', 'Analyze', 'Kill', 'Refresh') if hasattr(exp, name))


def _identifier(name: str, dialect: Optional[str]) -> exp.Identifier:
    """An identifier for an exact name, quoted when it is not a plain word, is a keyword or would change case."""
    return _names.identifier(name, dialect, exact_case=True)


def _table_name(text: str, dialect: Optional[str]) -> str:
    """A table name from ``Definition.tables`` (SQL text) as the dotted exact name used here.

    ``main."Order Items"`` -> ``main.Order Items``; ``order items`` (not SQL) is kept as one exact name.
    """
    return '.'.join(_names.fold_name(name, quoted, dialect) for name, quoted in _names.table_parts(text, dialect))


def _table_expr(name: str, dialect: Optional[str]) -> exp.Table:
    """A table expression for ``name`` ('orders', 'main.orders' or 'catalog.schema.table')."""
    parts = name.split('.')
    if len(parts) > 3 or not all(parts):
        parts = [name]
    keys = ('catalog', 'db', 'this')[-len(parts):]
    return exp.Table(**{key: _identifier(part, dialect) for key, part in zip(keys, parts)})


def _table_sql(name: str, dialect: Optional[str]) -> str:
    return _table_expr(name, dialect).sql(dialect=dialect)


def _column_sql(table: str, column: str, dialect: Optional[str]) -> str:
    """``table.column`` (or ``column``) written as SQL, for messages: ``"order items"."unit price"``."""
    name = _identifier(column, dialect).sql(dialect=dialect)
    return f'{_table_sql(table, dialect)}.{name}' if table else name


def _key(table: str) -> str:
    """Identity of a table: its name without schema, lower case."""
    return table.strip().lower().rsplit('.', 1)[-1]


class _Tables:
    """Ordered, case-insensitive set of table names that keeps the first spelling seen."""

    def __init__(self, names: Iterable[str] = ()) -> None:
        self._names: Dict[str, str] = {}
        for name in names:
            self.add(name)

    def add(self, name: str) -> str:
        return self._names.setdefault(_key(name), name)

    def find(self, name: str) -> Optional[str]:
        return self._names.get(_key(name))

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and _key(name) in self._names

    def __iter__(self):
        return iter(list(self._names.values()))

    def __len__(self) -> int:
        return len(self._names)


# ---------------------------------------------------------------------------------------------- SQL fragments
def _parse(fragment: str, dialect: Optional[str]) -> Optional[exp.Expression]:
    """Parse a SQL fragment; None when it is empty or sqlglot cannot parse it."""
    if not fragment or not fragment.strip():
        return None
    try:
        tree = sqlglot.parse_one(fragment, read=dialect)
    except SqlglotError:
        return None
    return _names.fold(tree, dialect) if tree is not None else None


def _qualifier(column: exp.Column) -> str:
    return '.'.join(part for part in (column.catalog, column.db, column.table) if part)


def _outer_columns(tree: exp.Expression) -> List[exp.Column]:
    """Columns of the fragment itself; columns of subqueries inside it are left out."""
    return [c for c in tree.find_all(exp.Column)
            if c.name and not isinstance(c.this, exp.Star) and c.find_ancestor(exp.Select) is None]


def _conjuncts(tree: exp.Expression) -> List[exp.Expression]:
    """Top-level AND terms of a condition."""
    while isinstance(tree, exp.Paren):
        tree = tree.this
    if isinstance(tree, exp.And):
        return _conjuncts(tree.left) + _conjuncts(tree.right)
    return [tree]


def _fragment_problem(fragment: str, dialect: Optional[str]) -> str:
    """Why ``fragment`` must not be embedded in a check query ('' when it may be)."""
    try:
        tokens = sqlglot.tokenize(fragment, read=dialect)
    except SqlglotError:
        return 'it holds a statement separator ;' if ';' in fragment else ''   # the database will judge the rest
    depth = 0
    for token in tokens:
        if token.token_type == TokenType.SEMICOLON:
            return 'it holds a statement separator ;'
        depth += {TokenType.L_PAREN: 1, TokenType.R_PAREN: -1}.get(token.token_type, 0)
        if depth < 0:
            return 'its parentheses are unbalanced'
    if depth:
        return 'its parentheses are unbalanced'
    tree = _parse(fragment, dialect)
    if isinstance(tree, _STATEMENTS):
        return f'it is a {type(tree).__name__.upper()} statement, not an expression'
    return ''


def _refusal(fragments: Sequence[str], dialect: Optional[str]) -> str:
    """Detail for the first fragment that must not run, or ''."""
    for fragment in fragments:
        if fragment and fragment.strip():
            problem = _fragment_problem(fragment, dialect)
            if problem:
                return f'refused to run {fragment.strip()!r}: {problem}'
    return ''


def _embed(fragment: str) -> str:
    """Fragment text for a check query; a newline closes any line comment ('--', or '#' in MySQL) inside it."""
    text = fragment.strip()
    return text + '\n' if '--' in text or '#' in text else text


def _where(predicates: Sequence[str]) -> str:
    terms = [p for p in predicates if p and p.strip()]
    return ' WHERE ' + ' AND '.join(f'({_embed(p)})' for p in terms) if terms else ''


@dataclass
class _Refs:
    """What a definition refers to."""

    tables: List[str]                              # tables the check query reads (FROM / JOIN), base first
    referenced: List[str]                          # every referenced table, including subquery-only tables
    columns: List[Tuple[str, str]]                 # (table, column); table '' when the column is unqualified
    unparsed: List[str] = field(default_factory=list)


def _column_ref(ref: str, known: _Tables, dialect: Optional[str]) -> Tuple[str, str]:
    """A ``Definition.columns`` entry -> (table, column); table '' when the column is unqualified.

    The entry is SQL text (``"order items"."unit price"``); text that does not parse as one column is split
    as raw text by :func:`_split_column`.
    """
    tree = _parse(ref, dialect)
    if isinstance(tree, exp.Column) and isinstance(tree.this, exp.Identifier) and tree.name:
        qualifier = _qualifier(tree)
        return (known.find(qualifier) or qualifier) if qualifier else '', tree.name
    return _split_column(ref, known)


def _split_column(ref: str, known: _Tables) -> Tuple[str, str]:
    """'table.column' -> (table, column), preferring the longest known table name as the prefix."""
    ref = ref.strip()
    prefixes = [t for t in known if ref.lower().startswith(t.lower() + '.')]
    if prefixes:
        table = max(prefixes, key=len)
        return table, ref[len(table) + 1:]
    if '.' in ref:
        table, column = ref.split('.', 1)
        return table, column
    return '', ref


def _references(d: Definition, dialect: Optional[str], extra: Sequence[str] = ()) -> _Refs:
    """Tables and columns of ``d`` (and of ``extra`` predicates evaluated over its tables)."""
    known = _Tables(_table_name(t, dialect) for t in d.tables if t and t.strip())
    outer, inner = _Tables(), _Tables()
    columns: List[Tuple[str, str]] = []
    unparsed: List[str] = []
    for fragment in [d.expression, *d.conditions, *extra]:
        tree = _parse(fragment, dialect)
        if tree is None:
            if fragment and fragment.strip():
                unparsed.append(fragment)
            continue
        for column in _outer_columns(tree):
            qualifier = _qualifier(column)
            table = known.add(qualifier) if qualifier else ''
            if table:
                outer.add(table)
            columns.append((table, column.name))
        for node in tree.find_all(exp.Table):           # a bare fragment holds tables only inside subqueries
            name = '.'.join(p for p in (node.catalog, node.db, node.name) if p)
            if name:
                inner.add(name)
    for ref in d.columns:
        if ref and ref.strip():
            table, column = _column_ref(ref, known, dialect)
            columns.append((known.find(table) or table if table else '', column))
    from_tables = [t for t in known if t in outer or t not in inner]
    if not from_tables and len(known):
        from_tables = list(known)[:1]
    referenced = _Tables(known)
    for name in list(inner) + [t for t, _ in columns if t]:
        referenced.add(name)
    unique: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for table, column in columns:
        unique.setdefault((_key(table) if table else '', column.lower()), (table, column))
    return _Refs(from_tables, list(referenced), list(unique.values()), unparsed)


# ---------------------------------------------------------------------------------------------- database access
def _cursor_run(conn: Any, sql: str, fetch: str) -> Any:
    """Execute ``sql`` on a fresh cursor and return one row, all rows or the description."""
    cursor = conn.cursor()
    try:
        cursor.execute(sql)
        if fetch == 'one':
            return cursor.fetchone()
        if fetch == 'all':
            return cursor.fetchall()
        return cursor.description
    finally:
        try:
            cursor.close()
        except Exception:  # noqa: BLE001 - closing must not hide the real outcome
            pass


def _recover(conn: Any) -> None:
    """Leave a transaction that an error aborted (e.g. PostgreSQL) so that the next statement can run."""
    if isinstance(conn, sqlite3.Connection):
        return
    rollback = getattr(conn, 'rollback', None)
    if callable(rollback):
        try:
            rollback()
        except Exception:  # noqa: BLE001 - best effort
            pass


def _query_one(conn: Any, sql: str) -> Tuple[Optional[Sequence[Any]], str]:
    """Run ``sql`` and fetch its first row: (row, '') or (None, error text)."""
    try:
        return _cursor_run(conn, sql, 'one'), ''
    except Exception as exc:  # noqa: BLE001 - every DB-API driver has its own error classes
        _recover(conn)
        return None, f'{type(exc).__name__}: {exc}'


def _ensure_connection(conn: Any, dialect: Optional[str]) -> None:
    """Fail before recording anything when ``conn`` is not a working DB-API connection."""
    if not callable(getattr(conn, 'cursor', None)):
        raise TypeError(f'conn must be a DB-API 2.0 connection (e.g. sqlite3.connect(...)), got {type(conn).__name__}')
    sql = exp.select(exp.Literal.number(1)).sql(dialect=dialect)
    try:
        _cursor_run(conn, sql, 'one')
    except Exception as exc:  # noqa: BLE001 - driver-specific error classes
        _recover(conn)
        raise RuntimeError(f'the database connection cannot run {sql!r}: {exc}') from exc


def _infer_dialect(conn: Any) -> Optional[str]:
    """sqlglot dialect for a connection, from its driver module (None means generic SQL)."""
    if isinstance(conn, sqlite3.Connection):
        return 'sqlite'
    root = (type(conn).__module__ or '').split('.')[0]
    for prefix, dialect in _DRIVER_DIALECTS:
        if root.startswith(prefix):
            return dialect
    return None


def _count(row: Optional[Sequence[Any]]) -> int:
    return int(row[0]) if row and row[0] is not None else 0


def _show(value: Any) -> str:
    text = f'{value:.6g}' if isinstance(value, float) else str(value)
    return text if len(text) <= 40 else text[:37] + '...'


def _plural(n: int, noun: str) -> str:
    return f'{n} {noun}' + ('' if n == 1 else 's')


class _Catalog:
    """Column names per table (lower case; None when the table does not exist), looked up once per run."""

    def __init__(self, conn: Any, dialect: Optional[str]) -> None:
        self.conn, self.dialect = conn, dialect
        self._columns: Dict[str, Optional[FrozenSet[str]]] = {}

    def columns(self, table: str) -> Optional[FrozenSet[str]]:
        key = table.strip().lower()
        if key not in self._columns:
            self._columns[key] = self._lookup(table.strip())
        return self._columns[key]

    def has(self, table: str, column: str) -> bool:
        names = self.columns(table)
        if names is None:
            return False
        return column.lower() in names or (self.dialect == 'sqlite' and column.lower() in _SQLITE_ROWID)

    def _lookup(self, table: str) -> Optional[FrozenSet[str]]:
        if self.dialect == 'sqlite':
            names = self._pragma(table)
            if names is not None:
                return names or None             # PRAGMA lists no column for a missing table
        return self._probe(table)

    def _pragma(self, table: str) -> Optional[FrozenSet[str]]:
        """Columns via PRAGMA table_info; None when PRAGMA itself fails (e.g. not a SQLite connection)."""
        def quote(text: str) -> str:
            return '"' + text.replace('"', '""') + '"'
        parts = table.split('.')
        if len(parts) == 2 and all(parts):
            sql = f'PRAGMA {quote(parts[0])}.table_info({quote(parts[1])})'
        else:
            sql = f'PRAGMA table_info({quote(table)})'
        try:
            rows = _cursor_run(self.conn, sql, 'all')
        except Exception:  # noqa: BLE001 - fall back to the generic probe
            _recover(self.conn)
            return None
        return frozenset(str(row[1]).lower() for row in rows or ())

    def _probe(self, table: str) -> Optional[FrozenSet[str]]:
        """Columns via SELECT * ... LIMIT 0 and cursor.description; None when the query fails."""
        sql = exp.select('*').from_(_table_expr(table, self.dialect)).limit(0).sql(dialect=self.dialect)
        try:
            description = _cursor_run(self.conn, sql, 'description')
        except Exception:  # noqa: BLE001 - a failing probe means the table cannot be read
            _recover(self.conn)
            return None
        return frozenset(str(col[0]).lower() for col in description or ())


@dataclass
class _Context:
    conn: Any
    dialect: Optional[str]
    catalog: _Catalog


# ---------------------------------------------------------------------------------------------- joins
@dataclass
class _JoinInfo:
    """A JOIN definition taken apart: its tables and its column = column terms."""

    tables: List[str]
    pairs: List[Tuple[exp.Column, exp.Column]]
    error: str = ''

    @property
    def self_join(self) -> bool:
        return len(self.tables) == 1


def _join_info(d: Definition, dialect: Optional[str]) -> _JoinInfo:
    tree = _parse(d.expression, dialect)
    if tree is None:
        return _JoinInfo([], [], f'could not parse the join condition {d.expression!r}')
    known = _Tables(_table_name(t, dialect) for t in d.tables if t and t.strip())
    tables = _Tables()
    for column in _outer_columns(tree):
        qualifier = _qualifier(column)
        if qualifier:
            tables.add(known.find(qualifier) or qualifier)
    names = list(tables)
    terms = _conjuncts(tree)
    pairs = [(t.left, t.right) for t in terms
             if isinstance(t, exp.EQ) and isinstance(t.left, exp.Column) and isinstance(t.right, exp.Column)]
    if len(names) == 2:
        pairs = [(a, b) for a, b in pairs if _key(_qualifier(a)) != _key(_qualifier(b))]
        return _JoinInfo(names, pairs)
    if len(names) == 1 and pairs and len(pairs) == len(terms):
        return _JoinInfo(names, pairs)
    found = ', '.join(names) or 'no qualified column'
    return _JoinInfo(names, [], f'the join condition {d.expression!r} must equate columns of two tables '
                                f'(or of one table in a self-join); found {found}')


def _on_clause(d: Definition) -> str:
    terms = [_embed(p) for p in [d.expression, *d.conditions] if p and p.strip()]
    return terms[0] if len(terms) == 1 else ' AND '.join(f'({t})' for t in terms)


def _self_join_on(info: _JoinInfo, dialect: Optional[str]) -> str:
    """ON clause of a self-join with the two sides renamed to ledger_l / ledger_r."""
    terms = []
    for left, right in info.pairs:
        sides = []
        for column, alias in ((left, _LEFT_ALIAS), (right, _RIGHT_ALIAS)):
            renamed = column.copy()
            renamed.set('catalog', None)
            renamed.set('db', None)
            renamed.set('table', exp.to_identifier(alias))
            sides.append(renamed)
        terms.append(exp.EQ(this=sides[0], expression=sides[1]).sql(dialect=dialect))
    return ' AND '.join(terms)


@dataclass
class _JoinEdge:
    definition_id: str
    tables: Tuple[str, str]
    on: str


class _JoinGraph:
    """Tables connected by usable JOIN definitions; edges are kept in preference order."""

    def __init__(self, edges: Sequence[_JoinEdge]) -> None:
        self._adjacent: Dict[str, List[Tuple[str, _JoinEdge]]] = {}
        for edge in edges:
            a, b = (_key(t) for t in edge.tables)
            self._adjacent.setdefault(a, []).append((b, edge))
            self._adjacent.setdefault(b, []).append((a, edge))

    def path(self, connected: Sequence[str], target: str) -> Optional[List[Tuple[str, _JoinEdge]]]:
        """Shortest chain of joins from any connected table to ``target``: [(new table key, edge), ...]."""
        start = [_key(t) for t in connected]
        goal = _key(target)
        previous: Dict[str, Tuple[str, _JoinEdge]] = {}
        seen: Set[str] = set(start)
        queue = deque(start)
        while queue and goal not in seen:
            node = queue.popleft()
            for neighbour, edge in self._adjacent.get(node, ()):
                if neighbour not in seen:
                    seen.add(neighbour)
                    previous[neighbour] = (node, edge)
                    queue.append(neighbour)
        if goal not in seen:
            return None
        chain: List[Tuple[str, _JoinEdge]] = []
        node = goal
        while node in previous:
            parent, edge = previous[node]
            chain.append((node, edge))
            node = parent
        return list(reversed(chain))


def _ledger_joins(ledger: Ledger, dialect: Optional[str], own: Optional[Definition] = None) -> List[_JoinEdge]:
    """Usable JOIN definitions as edges: not falsified, valid now, two tables; best evidence first.

    ``own`` (a JOIN definition under a counterexample check) is always used, and first.
    """
    usable = [d for d in ledger if d.kind == Kind.JOIN and not d.falsified and d.is_valid()
              and (own is None or d.id != own.id)]
    usable.sort(key=lambda d: (-int(d.grade), -d.support, d.id))
    if own is not None:
        usable.insert(0, own)
    edges = []
    for d in usable:
        if _refusal([d.expression, *d.conditions], dialect):
            continue
        info = _join_info(d, dialect)
        if not info.error and len(info.tables) == 2:
            edges.append(_JoinEdge(d.id, (info.tables[0], info.tables[1]), _on_clause(d)))
    return edges


def _from_clause(tables: Sequence[str], graph: Optional[_JoinGraph], dialect: Optional[str]) -> Tuple[str, str]:
    """FROM clause body reading ``tables``, joined through ``graph``: (sql, '') or ('', problem)."""
    if not tables:
        return '', 'the definition names no table'
    spelled = _Tables(tables)
    base = tables[0]
    parts = [_table_sql(base, dialect)]
    connected = [_key(base)]
    unreachable = []
    for table in tables[1:]:
        if _key(table) in connected:
            continue
        chain = graph.path(connected, table) if graph is not None else None
        if chain is None:
            unreachable.append(table)
            continue
        for node, edge in chain:
            name = spelled.find(node) or next(t for t in edge.tables if _key(t) == node)
            parts.append(f'JOIN {_table_sql(name, dialect)} ON {edge.on}')
            connected.append(node)
    if unreachable:
        names = ', '.join(_table_sql(t, dialect) for t in unreachable)
        return '', f'no JOIN definition in the ledger connects {names} with {_table_sql(base, dialect)}'
    return ' '.join(parts), ''


# ---------------------------------------------------------------------------------------------- the checks
def _check_columns(d: Definition, refs: _Refs, ctx: _Context) -> CheckResult:
    name = 'columns_exist'
    if not refs.referenced:
        return CheckResult(d.id, name, False, 'the definition names no table')
    missing_tables = [t for t in refs.referenced if ctx.catalog.columns(t) is None]
    missing_columns = []
    for table, column in refs.columns:
        if table:
            if ctx.catalog.columns(table) is not None and not ctx.catalog.has(table, column):
                missing_columns.append(_column_sql(table, column, ctx.dialect))
        elif not any(ctx.catalog.has(t, column) for t in refs.tables):
            missing_columns.append(_column_sql('', column, ctx.dialect))
    if missing_tables or missing_columns:
        problems = []
        if missing_tables:
            problems.append('missing tables: ' + ', '.join(_table_sql(t, ctx.dialect) for t in missing_tables))
        if missing_columns:
            problems.append('missing columns: ' + ', '.join(missing_columns))
        return CheckResult(d.id, name, False, '; '.join(problems))
    shown = [_column_sql(t, c, ctx.dialect) for t, c in refs.columns]
    tables = ', '.join(_table_sql(t, ctx.dialect) for t in refs.referenced)
    detail = f"tables {tables}; columns {', '.join(shown) or '-'}"
    if refs.unparsed:
        detail += '; not parsed, left to the SQL checks: ' + ', '.join(refs.unparsed)
    return CheckResult(d.id, name, True, detail)


def _check_executes(d: Definition, refs: _Refs, ctx: _Context, graph: Optional[_JoinGraph]) -> CheckResult:
    name = 'executes'
    source, problem = _from_clause(refs.tables, graph, ctx.dialect)
    problem = _refusal([d.expression, *d.conditions], ctx.dialect) or problem
    if problem:
        return CheckResult(d.id, name, False, problem)
    where = _where(d.conditions)
    sql = f'SELECT {_embed(d.expression)} FROM {source}{where}'
    row, error = _query_one(ctx.conn, sql)
    if error:
        return CheckResult(d.id, name, False, f'{error} [{sql}]')
    if row is None:
        return CheckResult(d.id, name, False, f'returned no row [{sql}]')
    if row[0] is None:
        return CheckResult(d.id, name, False, f'returned NULL [{sql}]')
    count_sql = f'SELECT COUNT(*) FROM {source}{where}'          # COUNT(...) gives 0, not NULL, over no rows
    count, error = _query_one(ctx.conn, count_sql)
    if error:
        return CheckResult(d.id, name, False, f'{error} [{count_sql}]')
    if _count(count) == 0:
        return CheckResult(d.id, name, False, f'returned {_show(row[0])} but reads no rows [{count_sql}]')
    return CheckResult(d.id, name, True, f'returned {_show(row[0])} [{sql}]')


def _check_selects_rows(d: Definition, refs: _Refs, ctx: _Context, graph: Optional[_JoinGraph]) -> CheckResult:
    name = 'selects_rows'
    source, problem = _from_clause(refs.tables, graph, ctx.dialect)
    problem = _refusal([d.expression, *d.conditions], ctx.dialect) or problem
    if problem:
        return CheckResult(d.id, name, False, problem)
    sql = f'SELECT COUNT(*) FROM {source}{_where([d.expression, *d.conditions])}'
    row, error = _query_one(ctx.conn, sql)
    if error:
        return CheckResult(d.id, name, False, f'{error} [{sql}]')
    n = _count(row)
    return CheckResult(d.id, name, n > 0, f'selects {_plural(n, "row")} [{sql}]')


def _check_join(d: Definition, ctx: _Context) -> List[CheckResult]:
    info = _join_info(d, ctx.dialect)
    problem = _refusal([d.expression, *d.conditions], ctx.dialect) or info.error
    if problem:
        return [CheckResult(d.id, 'joins_rows', False, problem),
                CheckResult(d.id, 'key_cardinality', False, problem)]
    if info.self_join:
        table = _table_sql(info.tables[0], ctx.dialect)
        source = f'{table} {_LEFT_ALIAS} JOIN {table} {_RIGHT_ALIAS} ON {_self_join_on(info, ctx.dialect)}'
        where = ''
    else:
        source = (f'{_table_sql(info.tables[0], ctx.dialect)} JOIN {_table_sql(info.tables[1], ctx.dialect)} '
                  f'ON {_embed(d.expression)}')
        where = _where(d.conditions)
    sql = f'SELECT COUNT(*) FROM {source}{where}'
    row, error = _query_one(ctx.conn, sql)
    if error:
        rows = CheckResult(d.id, 'joins_rows', False, f'{error} [{sql}]')
    else:
        n = _count(row)
        rows = CheckResult(d.id, 'joins_rows', n > 0, f'join returns {_plural(n, "row")} [{sql}]')
    return [rows, _check_key_cardinality(d, info, ctx)]


def _side_columns(pairs: Sequence[Tuple[exp.Column, exp.Column]], table: str) -> List[exp.Column]:
    columns: Dict[str, exp.Column] = {}
    for column in (c for pair in pairs for c in pair):
        if _key(_qualifier(column)) == _key(table):
            columns.setdefault(column.name.lower(), column)
    return list(columns.values())


def _duplicate_keys(ctx: _Context, table: str, columns: Sequence[exp.Column]) -> Tuple[Optional[int], str]:
    """Number of key values that occur more than once in ``table`` (NULL keys ignored)."""
    key = ', '.join(c.sql(dialect=ctx.dialect) for c in columns)
    not_null = ' AND '.join(f'{c.sql(dialect=ctx.dialect)} IS NOT NULL' for c in columns)
    sql = (f'SELECT COUNT(*) FROM (SELECT {key} FROM {_table_sql(table, ctx.dialect)} WHERE {not_null} '
           f'GROUP BY {key} HAVING COUNT(*) > 1) {_DUPLICATES_ALIAS}')
    row, error = _query_one(ctx.conn, sql)
    return (None, f'{error} [{sql}]') if error else (_count(row), '')


def _check_key_cardinality(d: Definition, info: _JoinInfo, ctx: _Context) -> CheckResult:
    name = 'key_cardinality'
    if not info.pairs:
        return CheckResult(d.id, name, False, 'the join condition equates no columns, so neither side is a key')
    if info.self_join:
        sides = [(info.tables[0], [a for a, _ in info.pairs]), (info.tables[0], [b for _, b in info.pairs])]
    else:
        sides = [(t, _side_columns(info.pairs, t)) for t in info.tables]
    unique, repeated, errors = [], [], []
    for table, columns in sides:
        names = ', '.join(_identifier(c.name, ctx.dialect).sql(dialect=ctx.dialect) for c in columns)
        label = f'{_table_sql(table, ctx.dialect)}({names})'
        duplicates, error = _duplicate_keys(ctx, table, columns)
        if error:
            errors.append(f'{label}: {error}')
        elif duplicates == 0:
            unique.append(label)
        else:
            repeated.append(f'{label} has {_plural(duplicates or 0, "repeated key")}')
    if unique:
        detail = 'unique: ' + ', '.join(unique)
        if repeated or errors:
            detail += '; not unique: ' + '; '.join(repeated + errors)
        return CheckResult(d.id, name, True, detail)
    return CheckResult(d.id, name, False, 'neither side is unique (many-to-many): ' + '; '.join(repeated + errors))


def _check_definition(d: Definition, ctx: _Context, graph: Optional[_JoinGraph]) -> List[CheckResult]:
    refs = _references(d, ctx.dialect)
    results = [_check_columns(d, refs, ctx)]
    if d.kind in (Kind.MEASURE, Kind.METRIC):
        results.append(_check_executes(d, refs, ctx, graph))
    elif d.kind == Kind.FILTER:
        results.append(_check_selects_rows(d, refs, ctx, graph))
    elif d.kind == Kind.JOIN:
        results.extend(_check_join(d, ctx))
    return results


def _targets(ledger: Ledger, definition_ids: Optional[Iterable[str]]) -> List[Definition]:
    """Definitions to check, in input order, each once; KeyError for an unknown id before anything runs."""
    if definition_ids is None:
        return list(ledger)
    if isinstance(definition_ids, str):
        definition_ids = [definition_ids]
    targets: Dict[str, Definition] = {}
    for definition_id in definition_ids:
        if definition_id not in targets:
            targets[definition_id] = ledger.get(definition_id)
    return list(targets.values())


def run_checks(ledger: Ledger, conn: Any, definition_ids: Optional[Iterable[str]] = None,
               dialect: str = 'sqlite') -> List[CheckResult]:
    """Run the data checks for the definitions (all, or ``definition_ids``) and record them as evidence.

    Raises ValueError for an unknown dialect, KeyError for an unknown id, TypeError when ``conn`` is not a
    DB-API connection and RuntimeError when it cannot run a query; in these cases nothing is recorded.
    """
    sqlglot.Dialect.get_or_raise(dialect)
    targets = _targets(ledger, definition_ids)
    if not targets:
        return []
    _ensure_connection(conn, dialect)
    ctx = _Context(conn, dialect, _Catalog(conn, dialect))
    results: Dict[str, List[CheckResult]] = {}
    graph: Optional[_JoinGraph] = None
    for d in sorted(targets, key=lambda t: t.kind != Kind.JOIN):    # JOINs first, otherwise input order
        if d.kind != Kind.JOIN and graph is None:
            graph = _JoinGraph(_ledger_joins(ledger, dialect))
        results[d.id] = _check_definition(d, ctx, graph)
        for r in results[d.id]:
            ledger.add_evidence(d.id, Evidence(EvidenceKind.DATA_CHECK, detail=r.detail, passed=r.passed, ref=r.name))
    return [r for d in targets for r in results[d.id]]


def _violations(violations: Iterable[str]) -> List[str]:
    if isinstance(violations, str):
        violations = [violations]
    predicates: List[str] = []
    for v in violations:
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f'a violation must be a non-empty SQL predicate, got {v!r}')
        if v.strip() not in predicates:
            predicates.append(v.strip())
    return predicates


def counterexample_checks(ledger: Ledger, conn: Any, definition_id: str, violations: Iterable[str]) -> List[CheckResult]:
    """Count the rows each violation predicate finds over the definition's tables; pass when there are none.

    Each result is named by its predicate and recorded as Evidence(kind=COUNTEREXAMPLE). Raises KeyError for an
    unknown id and ValueError (recording nothing) when a predicate is empty or cannot be evaluated.
    """
    d = ledger.get(definition_id)
    predicates = _violations(violations)
    if not predicates:
        return []
    dialect = _infer_dialect(conn)
    refused = _refusal(predicates, dialect)
    if refused:
        raise ValueError(f'counterexample checks for {d.name!r} were not run: {refused}')
    _ensure_connection(conn, dialect)
    refs = _references(d, dialect, extra=predicates)
    graph = _JoinGraph(_ledger_joins(ledger, dialect, own=d if d.kind == Kind.JOIN else None))
    source, problem = _from_clause(refs.tables, graph, dialect)
    if problem:
        raise ValueError(f'cannot run counterexample checks for {d.name!r}: {problem}')
    results: List[CheckResult] = []
    failures: List[str] = []
    for predicate in predicates:
        sql = f'SELECT COUNT(*) FROM {source} WHERE ({_embed(predicate)})'
        row, error = _query_one(conn, sql)
        if error:
            failures.append(f'{predicate!r}: {error}')
            continue
        n = _count(row)
        results.append(CheckResult(d.id, predicate, n == 0, f'{_plural(n, "violating row")} [{sql}]'))
    if failures:
        raise ValueError('counterexample checks could not run, nothing was recorded: ' + '; '.join(failures))
    for r in results:
        ledger.add_evidence(d.id, Evidence(EvidenceKind.COUNTEREXAMPLE, detail=r.detail, passed=r.passed, ref=r.name))
    return results
