"""Answer engine: SQL is assembled by rules from ledger definitions (the model only chooses definitions).

CONTRACT (implemented by the build step):

    assemble_sql(ledger, measure_id, filter_ids=(), group_by=(), dialect='sqlite') -> str
        measure_id names a MEASURE or METRIC definition; its conditions and the filters' expressions are
        ANDed into WHERE. FROM uses the measure's tables; any other table referenced by filters or group_by
        must be connected through JOIN definitions in the ledger (shortest path), else ValueError.
        group_by items are qualified column names ("customers.region"). The result is parsed with sqlglot
        as a static check before it is returned.
    answer(question, ledger, ...)   planned: choosing definitions for a natural-language question needs a model

How the SQL is assembled
------------------------
* Every SQL fragment (expression, condition, join condition, group_by item) is read, and the result is
  written, in ``dialect``. A fragment must be one SQL expression, and a predicate may not contain an
  aggregate outside a sub-query (HAVING filters are not supported yet).
* The tables a definition needs are its ``tables`` plus every table that qualifies one of its columns
  outside sub-queries (correlated references included); tables used only inside a sub-query are not
  joined. ``tables`` are SQL text in ``dialect``, quoted where needed (``"order items"``); text that is
  not a plain name (``order items`` without quotes) is taken as one exact name. Table names are matched
  case-insensitively, part by part, so ``"Order Items"`` and ``"order items"`` are the same table.
* FROM is the measure's first table. Each other needed table (the measure's other tables first, then the
  rest in name order) is attached along a shortest path found by breadth-first search over the JOIN
  definitions in the ledger, visiting neighbours in name order. Between two tables the JOIN definition
  with the highest grade, then the most support, then the smallest id is used; JOIN definitions that do not
  connect exactly two tables, or whose SQL does not parse, are ignored. All joins are inner joins, and
  fan-out through one-to-many joins is not corrected.
* WHERE ANDs the measure's conditions and, for each filter (sorted by name, then id), its expression and
  its own conditions; a repeated predicate appears once.
* SELECT lists the group_by columns in the given order and then the measure under its name (a quoted
  alias); GROUP BY and ORDER BY use the group_by columns, so rows come back in a fixed order.
* Errors: unknown ids raise KeyError; a wrong definition kind, a bad fragment or group_by item, an unknown
  dialect or an unconnected table raise ValueError; a bare string where a list is expected raises TypeError.

:func:`assemble` returns the same SQL together with the ids of every definition used (measure, filters,
joins) and the tables read, e.g. for :func:`sql_semantic_ledger.grade.decide` or for a
:class:`sql_semantic_ledger.revalidate.AnswerRecord`.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from . import _names
from ._unimplemented import planned
from .model import Definition, Kind, Ledger

# (table key, JOIN definition): a table attached to the query, or a BFS parent link (previous table, JOIN)
_Step = Tuple[str, Definition]


@dataclass
class AssembledQuery:
    """SQL assembled from ledger definitions, with the definitions and tables it uses."""

    sql: str
    definitions: List[str] = field(default_factory=list)   # measure id, filter ids, then JOIN ids in join order
    tables: List[str] = field(default_factory=list)        # FROM table, then the joined tables (SQL text)


@dataclass
class _Parsed:
    """A definition's SQL parsed once: expression, conditions and the tables it needs."""

    definition: Definition
    expression: exp.Expression
    conditions: List[exp.Expression]
    tables: List[str]                                      # SQL text, quoted where needed


# ---------------------------------------------------------------------- small helpers
def _key(table: str, dialect: Any) -> str:
    """Key under which a table name written as SQL text is matched: its unquoted parts, lower case."""
    return _names.table_key(table, dialect)


def _column_key(column: exp.Column) -> str:
    """Key of the table that qualifies ``column`` (same form as :func:`_key`)."""
    return '.'.join(name.lower() for name, _ in _names.column_table_parts(column))


def _qualifier_sql(column: exp.Column, dialect: Any) -> str:
    """The table that qualifies ``column`` as SQL text, quoted as in the column (``"order items"``)."""
    return _names.parts_table(_names.column_table_parts(column), dialect).sql(dialect=dialect)


def _first_line(error: Exception) -> str:
    text = str(error).strip()
    return text.splitlines()[0] if text else type(error).__name__


def _describe(d: Definition) -> str:
    return f'{d.kind.value} {d.name!r} [{d.id}]'


def _strings(name: str, values: Iterable[str]) -> List[str]:
    """``values`` as a list of strings; a bare string is refused (it would be read as characters)."""
    if isinstance(values, (str, bytes)):
        raise TypeError(f'{name} must be a collection of strings, not a single string: {values!r}')
    items = list(values)
    for item in items:
        if not isinstance(item, str):
            raise TypeError(f'{name} must contain strings, got {item!r}')
    return items


def _unique_tables(names: Iterable[str], dialect: Any) -> List[str]:
    """Non-empty table names without case-insensitive repeats; the first spelling is kept."""
    seen: Set[str] = set()
    out: List[str] = []
    for name in names:
        if isinstance(name, str) and name.strip() and _key(name, dialect) not in seen:
            seen.add(_key(name, dialect))
            out.append(name.strip())
    return out


def _unique_nodes(nodes: Iterable[exp.Expression], dialect: Any) -> List[exp.Expression]:
    """Expressions without repeats (compared by their SQL text); the first occurrence is kept."""
    seen: Set[str] = set()
    out: List[exp.Expression] = []
    for node in nodes:
        text = node.sql(dialect=dialect)
        if text not in seen:
            seen.add(text)
            out.append(node)
    return out


def _check_dialect(dialect: Any) -> None:
    try:
        sqlglot.Dialect.get_or_raise(dialect)
    except ValueError:
        raise ValueError(f'unknown SQL dialect {dialect!r}') from None


def _lookup(ledger: Ledger, definition_id: str, role: str) -> Definition:
    if not isinstance(definition_id, str):
        raise TypeError(f'{role}: definition ids are strings, got {definition_id!r}')
    if definition_id not in ledger:
        raise KeyError(f'{role}: no definition with id {definition_id!r} in the ledger')
    return ledger.get(definition_id)


def _table(name: str, dialect: Any) -> exp.Table:
    """A table expression for a name written as SQL text (an unparsable name is taken as one exact name)."""
    return _names.parts_table(_names.table_parts(name, dialect), dialect)


# ---------------------------------------------------------------------- parsing definitions
def _parse_fragment(text: str, where: str, dialect: Any, predicate: bool) -> exp.Expression:
    """Parse one stored SQL fragment; it must be a single SQL expression (an alias on it is dropped)."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f'{where} is empty')
    try:
        trees = [t for t in sqlglot.parse(text, read=dialect) if t is not None]
    except SqlglotError as e:
        raise ValueError(f'{where} does not parse: {_first_line(e)}') from None
    if len(trees) != 1:
        raise ValueError(f'{where} must be one SQL expression, found {len(trees)} statements: {text!r}')
    node = trees[0].unalias()
    if not isinstance(node, exp.Condition):
        raise ValueError(f'{where} is not a SQL expression: {text!r}')
    if predicate and any(agg.find_ancestor(exp.Select) is None for agg in node.find_all(exp.AggFunc)):
        raise ValueError(f'{where} has an aggregate outside a sub-query; HAVING filters are not supported: {text!r}')
    return node


def _bound_inside(column: exp.Column) -> bool:
    """True if the column's qualifier is bound by a sub-query that encloses the column.

    A table read in a sub-query binds its alias, or its name when it has no alias; so in
    ``(SELECT 1 FROM customers AS c2 WHERE c2.region = customers.region)`` the qualifier ``customers``
    still refers to the outer query.
    """
    key = column.table.lower()
    scope = column.find_ancestor(exp.Select)
    while scope is not None:
        if any(table.alias_or_name.lower() == key for table in scope.find_all(exp.Table)):
            return True
        scope = scope.find_ancestor(exp.Select)
    return False


def _outer_tables(node: exp.Expression, dialect: Any) -> List[str]:
    """Tables qualifying columns of ``node`` outside its sub-queries (correlated references included), sorted."""
    names = _unique_tables((_qualifier_sql(col, dialect) for col in node.find_all(exp.Column)
                            if col.table and not _bound_inside(col)), dialect)
    return sorted(names, key=lambda name: _key(name, dialect))


def _subquery_tables(node: exp.Expression) -> Set[str]:
    """Keys of the tables read inside sub-queries of a fragment (a fragment has no FROM of its own)."""
    return {t.name.lower() for t in node.find_all(exp.Table) if t.name}


def _parse_definition(d: Definition, dialect: Any, expression_is_predicate: bool) -> _Parsed:
    """Parse a definition's expression and conditions and work out the tables it needs."""
    expression = _parse_fragment(d.expression, f'expression of {_describe(d)}', dialect, expression_is_predicate)
    conditions = [_parse_fragment(c, f'condition {i + 1} of {_describe(d)}', dialect, True)
                  for i, c in enumerate(d.conditions)]
    nodes = [expression] + conditions
    referenced = [t for node in nodes for t in _outer_tables(node, dialect)]
    inner_only = {k for node in nodes for k in _subquery_tables(node)} - {_key(t, dialect) for t in referenced}
    declared = [_names.table_sql(t, dialect) for t in d.tables if isinstance(t, str) and t.strip()]
    declared = [t for t in declared if _key(t, dialect) not in inner_only]
    return _Parsed(d, expression, conditions, _unique_tables(declared + referenced, dialect))


def _group_column(item: str, dialect: Any) -> exp.Column:
    """Parse a group_by item; it must be a qualified column name such as ``customers.region``."""
    try:
        trees = [t for t in sqlglot.parse(item, read=dialect) if t is not None] if item.strip() else []
    except SqlglotError:
        trees = []
    node = trees[0] if len(trees) == 1 else None
    if not (isinstance(node, exp.Column) and node.table and isinstance(node.this, exp.Identifier)):
        raise ValueError(f'group_by item {item!r} is not a qualified column name such as "customers.region"')
    return node


# ---------------------------------------------------------------------- joins
class _JoinGraph:
    """Tables as nodes and the ledger's JOIN definitions as edges (the best definition per pair of tables)."""

    def __init__(self, ledger: Ledger, dialect: Any) -> None:
        self.edges: Dict[str, Dict[str, Definition]] = {}
        self.on: Dict[str, exp.Expression] = {}       # JOIN id -> ON condition
        self.names: Dict[str, str] = {}               # table key -> spelling in the JOIN definition
        self.ignored: List[str] = []                  # JOIN ids that cannot serve as an edge
        rank: Dict[str, Tuple[int, int, str]] = {}
        for d in sorted(ledger.find(kind=Kind.JOIN), key=lambda j: j.id):
            try:
                parsed: Optional[_Parsed] = _parse_definition(d, dialect, expression_is_predicate=True)
            except ValueError:
                parsed = None
            if parsed is None or len(parsed.tables) != 2:
                self.ignored.append(d.id)
                continue
            rank[d.id] = (-int(d.grade), -d.support, d.id)
            self.on[d.id] = exp.and_(parsed.expression, *parsed.conditions)
            for table in parsed.tables:
                self.names.setdefault(_key(table, dialect), table)
            a, b = (_key(t, dialect) for t in parsed.tables)
            for x, y in ((a, b), (b, a)):
                best = self.edges.setdefault(x, {}).get(y)
                if best is None or rank[d.id] < rank[best.id]:
                    self.edges[x][y] = d

    def shortest_path(self, sources: Sequence[str], target: str) -> Optional[List[_Step]]:
        """Breadth-first search from any of ``sources`` to ``target``: the (table, JOIN) steps, or None."""
        parent: Dict[str, Optional[_Step]] = {s: None for s in sources}
        queue = deque(sources)
        while queue and target not in parent:
            node = queue.popleft()
            for neighbour in sorted(self.edges.get(node, {})):
                if neighbour not in parent:
                    parent[neighbour] = (node, self.edges[node][neighbour])
                    queue.append(neighbour)
        if target not in parent:
            return None
        steps: List[_Step] = []
        node = target
        link = parent[node]
        while link is not None:
            previous, join = link
            steps.append((node, join))
            node = previous
            link = parent[node]
        steps.reverse()
        return steps


def _needed_tables(measure: _Parsed, filters: Sequence[_Parsed], columns: Sequence[exp.Column],
                   dialect: Any) -> Tuple[Dict[str, str], Dict[str, List[str]]]:
    """Spelling and requesters of every table the query needs (keyed by case-insensitive name)."""
    requests = [(t, f'measure {measure.definition.name!r}') for t in measure.tables]
    requests += [(t, f'filter {p.definition.name!r}') for p in filters for t in p.tables]
    requests += [(_qualifier_sql(c, dialect), f'group_by {c.sql(dialect=dialect)}') for c in columns]
    names: Dict[str, str] = {}
    needs: Dict[str, List[str]] = {}
    for table, who in requests:
        names.setdefault(_key(table, dialect), table)
        needs.setdefault(_key(table, dialect), []).append(who)
    return names, needs


def _connect(graph: _JoinGraph, root: str, targets: Sequence[str], names: Dict[str, str],
             needs: Dict[str, List[str]]) -> Tuple[List[str], List[_Step]]:
    """Attach each target table to the tables joined so far along a shortest JOIN path (FROM table = root)."""
    joined = [root]
    steps: List[_Step] = []
    for target in targets:
        if target in joined:
            continue
        path = graph.shortest_path(joined, target)
        if path is None:
            message = (f'table {names[target]!r} (needed by {", ".join(needs[target])}) is not connected to '
                       f'{", ".join(names[k] for k in joined)} through JOIN definitions in the ledger')
            if graph.ignored:
                message += f'; JOIN definitions ignored as edges: {", ".join(graph.ignored)}'
            raise ValueError(message)
        for table, join in path:
            joined.append(table)
            steps.append((table, join))
    return joined, steps


# ---------------------------------------------------------------------- rendering and checking
def _render(measure: _Parsed, columns: Sequence[exp.Column], predicates: Sequence[exp.Expression],
            names: Dict[str, str], root: str, steps: Sequence[_Step], graph: _JoinGraph, dialect: Any) -> str:
    alias = exp.to_identifier(str(measure.definition.name).strip() or 'value', quoted=True)
    query = exp.select(*(c.copy() for c in columns), exp.alias_(measure.expression.copy(), alias))
    query = query.from_(_table(names[root], dialect))
    for table, join in steps:
        query = query.join(_table(names[table], dialect), on=graph.on[join.id].copy())
    if predicates:
        query = query.where(exp.and_(*(p.copy() for p in predicates)))
    if columns:
        query = query.group_by(*(c.copy() for c in columns)).order_by(*(c.copy() for c in columns))
    return query.sql(dialect=dialect, pretty=True)


def _static_check(sql: str, dialect: Any, joined: Set[str]) -> None:
    """Parse the assembled SQL again: one SELECT whose column qualifiers are all joined tables."""
    try:
        trees = [t for t in sqlglot.parse(sql, read=dialect) if t is not None]
    except SqlglotError as e:
        raise ValueError(f'assembled SQL does not parse: {_first_line(e)}') from None
    if len(trees) != 1 or not isinstance(trees[0], exp.Select):
        raise ValueError('assembled SQL is not a single SELECT statement')
    select = trees[0]
    stray = sorted({_qualifier_sql(col, dialect) for col in select.find_all(exp.Column)
                    if col.table and col.find_ancestor(exp.Select) is select and _column_key(col) not in joined})
    if stray:
        raise ValueError(f'assembled SQL refers to tables that are not joined: {", ".join(stray)}')


# ---------------------------------------------------------------------- public API
def assemble(ledger: Ledger, measure_id: str, filter_ids: Iterable[str] = (), group_by: Iterable[str] = (),
             dialect: str = 'sqlite') -> AssembledQuery:
    """Assemble SQL for one measure or metric with filters and group-by columns.

    Same rules as :func:`assemble_sql` (see the module docstring); also returns the ids of every
    definition used, including the JOIN definitions chosen to connect the tables, and the tables read.
    """
    if not isinstance(ledger, Ledger):
        raise TypeError(f'ledger must be a Ledger, got {type(ledger).__name__}')
    _check_dialect(dialect)
    filter_list = _strings('filter_ids', filter_ids)
    group_list = _strings('group_by', group_by)
    measure = _lookup(ledger, measure_id, 'measure_id')
    if measure.kind not in (Kind.MEASURE, Kind.METRIC):
        raise ValueError(f'measure_id must name a measure or metric definition, got {_describe(measure)}')
    filters: Dict[str, Definition] = {}
    for filter_id in filter_list:
        f = _lookup(ledger, filter_id, 'filter_ids')
        if f.kind != Kind.FILTER:
            raise ValueError(f'filter_ids must name filter definitions, got {_describe(f)}')
        filters[f.id] = f

    measure_p = _parse_definition(measure, dialect, expression_is_predicate=False)
    if not measure_p.tables:
        raise ValueError(f'{_describe(measure)} names no table to select from')
    filter_ps = [_parse_definition(f, dialect, expression_is_predicate=True)
                 for f in sorted(filters.values(), key=lambda d: (d.name, d.id))]
    columns = _unique_nodes((_group_column(item, dialect) for item in group_list), dialect)

    names, needs = _needed_tables(measure_p, filter_ps, columns, dialect)
    graph = _JoinGraph(ledger, dialect)
    for key, spelling in graph.names.items():
        names.setdefault(key, spelling)
    root = _key(measure_p.tables[0], dialect)
    targets = list(dict.fromkeys([_key(t, dialect) for t in measure_p.tables[1:]] + sorted(needs)))
    joined, steps = _connect(graph, root, targets, names, needs)

    predicates = _unique_nodes([*measure_p.conditions,
                                *(node for p in filter_ps for node in (p.expression, *p.conditions))], dialect)
    sql = _render(measure_p, columns, predicates, names, root, steps, graph, dialect)
    _static_check(sql, dialect, set(joined))
    return AssembledQuery(sql=sql,
                          definitions=[measure.id] + [p.definition.id for p in filter_ps] + [j.id for _, j in steps],
                          tables=[names[k] for k in joined])


def assemble_sql(ledger: Ledger, measure_id: str, filter_ids: Iterable[str] = (), group_by: Iterable[str] = (),
                 dialect: str = 'sqlite') -> str:
    """SQL for one measure or metric with filters and group-by columns, assembled by rules from the ledger.

    ``measure_id`` names a MEASURE or METRIC definition, ``filter_ids`` FILTER definitions and
    ``group_by`` qualified column names. Tables outside the measure's tables are joined along the
    shortest path of JOIN definitions (ValueError if none connects them). The SQL is parsed again with
    sqlglot as a static check before it is returned. See the module docstring for the exact rules.
    """
    return assemble(ledger, measure_id, filter_ids, group_by, dialect).sql


def answer(question: str, ledger: Ledger, *args, **kwargs):
    """Answer a natural-language question with ledger definitions. Planned."""
    planned('trust.engine_nl')
