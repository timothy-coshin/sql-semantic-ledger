"""SQL parsing helpers built on sqlglot: statement splitting, normalization and feature extraction.

CONTRACT (implemented by the build step; keep names and fields stable):

    split_statements(text, dialect=None) -> list[str]
    parse_query(sql, dialect=None, query_id=None) -> QueryFeatures | None
        Parse one statement. Return None when it does not parse or is not a SELECT/WITH query.
        Aliases are resolved to table names in every fragment ("o.amount" -> "orders.amount").
        ``normalized`` is the pretty-printed canonical SQL; ``skeleton`` is the same SQL with every
        literal replaced by a placeholder, used to decide whether two queries are "different".
        ``aggregates``: SUM/COUNT/AVG/MIN/MAX(...) expressions (COUNT(DISTINCT x) kept as such).
        ``predicates``: atomic WHERE/HAVING conditions (=, <>, <, <=, >, >=, IN, LIKE, BETWEEN, IS [NOT] NULL),
                        split on top-level AND; OR groups kept as one predicate.
        ``joins``: equality join conditions "a.x = b.y" with both sides qualified, sides sorted.

Additions (used by :mod:`induce`, also available to other modules):

    analyze_query(sql, dialect=None, query_id=None, *, schema=None) -> QueryAnalysis | None
        parse_query plus what candidate generation needs: the aggregates and row-level predicates that may
        become definitions and, for every aggregate, the conditions that apply to the rows it aggregates.
        ``schema`` (table -> column names) only helps to attribute unqualified columns in multi-table scopes.
    normalize_expression(sql, dialect=None) -> str
        Canonical text of a standalone fragment that is already written with table names (for example a
        metric-store expression), in the same form as the fragments above.

How a query is normalized
-------------------------
* Identifiers are normalized the way the dialect resolves them (most dialects lower-case unquoted names)
  and quotes that are not needed are dropped, so ``ORDERS.Amount`` and ``"orders".amount`` agree.
* Columns are resolved scope by scope (``sqlglot.optimizer.scope``). A qualifier is looked up among the
  FROM/JOIN sources of the column's scope and then of the enclosing scopes (correlated subqueries). An
  unqualified column belongs to the only source of its scope or, with ``schema``, to the only source known
  to have it; otherwise it stays unqualified. Columns of CTEs and derived tables are traced to the base-table
  column they pass through (plain columns and ``*``). Computed outputs (aggregates, expressions) and sources
  that change the rows (GROUP BY, DISTINCT, LIMIT, window functions, set operations) are not traced, and
  fragments that use them are left out instead of being reported with a CTE or alias name; so is a subquery
  fragment that reads a CTE defined outside it. In a self-join both instances map to the table name, so a
  condition on one instance loses its role (``m.name = 'Ann'`` -> ``employees.name = 'Ann'``) and a condition
  that compares the two instances (``c.region = c2.region``) is left out.
* Select-list aliases used in GROUP BY / HAVING / ORDER BY and positional GROUP BY items are expanded.
* Canonical fragments: AND/OR operands are flattened, de-duplicated and sorted; a comparison puts the column
  side on the left (``'paid' = o.status`` -> ``orders.status = 'paid'``); literal IN lists are sorted;
  ``NOT x LIKE y`` becomes ``x NOT LIKE y``; COUNT(1) becomes COUNT(*); redundant parentheses are dropped.
* ``normalized`` applies the same resolution to the whole query: table and CTE aliases are replaced by their
  names (a table that occurs twice in one scope gets ``<table>_1``, ``<table>_2``; a table that also occurs in
  another scope keeps its aliases so correlated references stay unambiguous), comments are dropped and
  conditions are canonical, so queries that differ only in formatting, alias names, condition order or
  comments get the same ``normalized`` and ``skeleton``. ``skeleton`` replaces string, number and boolean
  literals by ``?`` and collapses literal IN lists to ``IN (?)``.
* Predicates are WHERE and HAVING conjuncts plus single-table conjuncts of INNER JOIN ... ON. An equality
  between plain columns of two different sources (in ON, in WHERE for comma joins, in USING, or between a
  correlated subquery and its outer query) is a join instead of a predicate. Correlated conditions, constant
  conditions (``1 = 1``) and the other conjuncts of OUTER JOIN ... ON are left out. HAVING conditions with an
  aggregate (``COUNT(*) > 5``) are predicates; :func:`analyze_query` keeps them out of ``filters``.
* ``Fragment.tables`` are the tables a fragment needs in FROM (for COUNT(*) all tables of its scope);
  ``Fragment.columns`` are all qualified columns it reads, including those inside subqueries. Both are SQL
  text in the parse dialect, quoted only where needed (``frpm."free meal count (k-12)"``).
"""
from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, FrozenSet, Iterator, List, Mapping, Optional, Set, Tuple

import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.scope import Scope, traverse_scope
from sqlglot.tokens import TokenType

logger = logging.getLogger(__name__)

#: Aggregate functions that become MEASURE fragments.
AGGREGATE_TYPES = (exp.Sum, exp.Count, exp.Avg, exp.Min, exp.Max)

_SET_OPERATIONS = tuple(t for t in (getattr(exp, 'SetOperation', None), exp.Union, exp.Intersect, exp.Except)
                        if t is not None)
_QUERY_NODES = (exp.Select, exp.Subquery) + _SET_OPERATIONS
_SEMICOLON = getattr(exp, 'Semicolon', None)
_MIRRORED = {exp.EQ: exp.EQ, exp.NEQ: exp.NEQ, exp.GT: exp.LT, exp.LT: exp.GT, exp.GTE: exp.LTE, exp.LTE: exp.GTE}
_ATOMS = (exp.Column, exp.Literal, exp.Null, exp.Boolean, exp.Placeholder, exp.Parameter, exp.Func, exp.Paren)
_CONSTANTS = (exp.Literal, exp.Null, exp.Boolean)
_PLAIN_NAME = re.compile(r'[a-z_][a-z0-9_]*\Z')
_SQL_RESERVED = frozenset('''
    ALL ALTER AND ANY AS ASC BETWEEN BY CASE CAST CHECK COLLATE COLUMN CONSTRAINT CREATE CROSS CURRENT
    CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP CURRENT_USER DEFAULT DELETE DESC DISTINCT DROP ELSE END EXCEPT
    EXISTS FALSE FETCH FOR FOREIGN FROM FULL GRANT GROUP HAVING IN INNER INSERT INTERSECT INTO IS JOIN LEFT
    LIKE LIMIT NATURAL NOT NULL OFFSET ON OR ORDER OUTER PRIMARY REFERENCES RIGHT ROW ROWS SELECT SESSION_USER
    SET SOME TABLE THEN TO TRUE UNION UNIQUE UPDATE USER USING VALUES WHEN WHERE WINDOW WITH'''.split())
_MAX_DEPTH = 16

# keys of the sqlglot ``Expression.meta`` annotations written while a query is analysed
_COLUMN_META = 'ledger_column'   # on Column nodes: how the column resolves
_ALIAS_META = 'ledger_alias'     # on Table nodes: how normalized SQL rewrites the alias
_CTE_META = 'ledger_cte'         # on Table nodes that reference a CTE

_BASE, _AMBIGUOUS, _UNRESOLVED, _STAR = 'base', 'ambiguous', 'unresolved', 'star'

Ident = Tuple[str, bool]          # identifier name and whether it is quoted
TableParts = Tuple[Ident, ...]    # catalog / db / table identifiers that are present, outermost first


@dataclass
class Fragment:
    sql: str                                        # normalized fragment, e.g. "SUM(orders.amount)"
    tables: List[str] = field(default_factory=list)
    columns: List[str] = field(default_factory=list)  # "table.column"


@dataclass
class QueryFeatures:
    query_id: str
    sql: str
    normalized: str
    skeleton: str
    tables: List[str] = field(default_factory=list)
    aggregates: List[Fragment] = field(default_factory=list)
    predicates: List[Fragment] = field(default_factory=list)
    joins: List[Fragment] = field(default_factory=list)
    group_by: List[str] = field(default_factory=list)


@dataclass
class MetricUse:
    """One aggregate of one query scope and the row-level conditions that apply to the rows it aggregates."""

    aggregate: Fragment
    conditions: List[Fragment] = field(default_factory=list)


@dataclass
class QueryAnalysis:
    """Result of :func:`analyze_query`: the features plus what candidate generation needs."""

    features: QueryFeatures
    measures: List[Fragment] = field(default_factory=list)      # aggregates without parameter placeholders
    filters: List[Fragment] = field(default_factory=list)       # row-level predicates without placeholders
    metric_uses: List[MetricUse] = field(default_factory=list)
    columns_by_table: Dict[str, List[str]] = field(default_factory=dict)  # table -> column names seen with it
    ambiguous: bool = False   # some unqualified column could not be attributed to one table


# ---------------------------------------------------------------------------------------------- public API
def split_statements(text: str, dialect: Optional[str] = None) -> List[str]:
    """Split SQL text into statements on top-level ``;``.

    Semicolons inside string literals, quoted identifiers and comments do not split. Each statement is
    returned stripped and without its semicolon; empty statements and statements that contain only comments
    are dropped. Text that the dialect's tokenizer rejects (e.g. an unterminated string) is split by a simple
    quote- and comment-aware scanner instead. Raises ValueError for an unknown dialect.
    """
    tokenizer = Dialect.get_or_raise(dialect)
    if not text or not text.strip():
        return []
    try:
        tokens = tokenizer.tokenize(text)
    except Exception:  # noqa: BLE001 - any tokenizer failure falls back to the plain scanner
        return _split_plain(text)
    pieces: List[str] = []
    start, has_tokens = 0, False
    for token in tokens:
        if token.token_type == TokenType.SEMICOLON:
            if has_tokens:
                pieces.append(text[start:token.start].strip())
            start, has_tokens = token.end + 1, False
        else:
            has_tokens = True
    if has_tokens:
        pieces.append(text[start:].strip())
    return [p for p in pieces if p]


def parse_query(sql: str, dialect: Optional[str] = None, query_id: Optional[str] = None) -> Optional[QueryFeatures]:
    """Features of one SELECT/WITH statement, or None if it does not parse or is not a query.

    Raises ValueError for an unknown dialect. See the module docstring for the normalization rules.
    """
    analysis = analyze_query(sql, dialect=dialect, query_id=query_id)
    return None if analysis is None else analysis.features


def analyze_query(sql: str, dialect: Optional[str] = None, query_id: Optional[str] = None, *,
                  schema: Optional[Mapping[str, Any]] = None) -> Optional[QueryAnalysis]:
    """Like :func:`parse_query`, plus the candidate inputs used by :mod:`induce`.

    ``schema`` maps table names (as in ``Fragment.tables``) to column names; it is only used to attribute an
    unqualified column in a scope with several sources to the one source that has it.
    """
    Dialect.get_or_raise(dialect)  # an unknown dialect is a usage error, not an unparsable query
    root = _parse_single_query(sql, dialect)
    if root is None:
        return None
    try:
        return _Analyzer(root, sql, dialect, query_id, schema).run()
    except (SqlglotError, RecursionError) as error:   # e.g. sqlglot's scope analysis rejects duplicate aliases
        logger.debug('skipping query %s: %s', query_id or '?', error)
    except Exception as error:  # noqa: BLE001 - one odd query must not stop the induction of a whole log
        logger.warning('skipping query %s: unexpected %s: %s', query_id or '?', type(error).__name__, error)
    return None


def normalize_expression(sql: str, dialect: Optional[str] = None) -> str:
    """Canonical text of a standalone SQL fragment (aggregate, predicate or join condition).

    The fragment must already use table names as qualifiers (no aliases to resolve); the result has the
    same form as the fragments produced by :func:`parse_query`, e.g. ``"'paid' = Orders.Status"`` ->
    ``"orders.status = 'paid'"``. Raises ValueError if the text does not parse or is a whole query.
    """
    Dialect.get_or_raise(dialect)
    try:
        node = sqlglot.parse_one(sql, read=dialect)
    except Exception as error:  # noqa: BLE001 - report every parser failure the same way
        raise ValueError(f'cannot parse SQL fragment {sql!r}: {error}') from None
    if node is None or isinstance(node, _QUERY_NODES):
        raise ValueError(f'expected a SQL fragment, not a query: {sql!r}')
    node = normalize_identifiers(node, dialect=dialect)
    _unquote_plain_identifiers(node, dialect)
    return _strip_parens(_canonical(node, dialect)).sql(dialect=dialect, comments=False)


# ---------------------------------------------------------------------------------------------- parsing
def _parse_single_query(sql: Any, dialect: Optional[str]) -> Optional[exp.Expression]:
    """The single SELECT / set-operation statement in ``sql``, or None."""
    if not isinstance(sql, str) or not sql.strip():
        return None
    try:
        parsed = sqlglot.parse(sql, read=dialect)
    except Exception:  # noqa: BLE001 - sqlglot reports unparsable text with several exception types
        return None
    statements = [s for s in parsed if s is not None and not (_SEMICOLON and isinstance(s, _SEMICOLON))]
    if len(statements) != 1:
        return None
    root = statements[0]
    while isinstance(root, exp.Subquery) and not any(v for k, v in root.args.items() if k != 'this'):
        root = root.this
    return root if isinstance(root, (exp.Select,) + _SET_OPERATIONS) else None


def _split_plain(text: str) -> List[str]:
    """Split on ``;`` outside quotes and comments (fallback for text the tokenizer rejects)."""
    pieces: List[str] = []
    buffer: List[str] = []
    has_code = False
    i, n = 0, len(text)
    while i < n:
        ch, nxt = text[i], text[i + 1:i + 2]
        if ch == '-' and nxt == '-':
            end = text.find('\n', i)
            end = n if end < 0 else end
        elif ch == '/' and nxt == '*':
            end = text.find('*/', i + 2)
            end = n if end < 0 else end + 2
        elif ch in '\'"`':
            end = i + 1
            while end < n and not (text[end] == ch and text[end + 1:end + 2] != ch):
                end += 2 if text[end] == ch else 1   # a doubled quote is an escaped quote
            end = min(end + 1, n)
            has_code = True
        elif ch == ';':
            if has_code:
                pieces.append(''.join(buffer).strip())
            buffer, has_code = [], False
            i += 1
            continue
        else:
            end = i + 1
            has_code = has_code or not ch.isspace()
        buffer.append(text[i:end])
        i = end
    if has_code:
        pieces.append(''.join(buffer).strip())
    return [p for p in pieces if p]


# ---------------------------------------------------------------------------------------------- analysis
@dataclass
class _Resolution:
    kind: str                              # _BASE | _AMBIGUOUS | _UNRESOLVED
    src_scope: Optional[Scope] = None      # scope whose FROM/JOIN provides the column's source
    src_alias: Optional[str] = None        # that source's name in src_scope
    table: Optional[TableParts] = None     # base table (kind _BASE)
    column: Optional[Ident] = None         # base column name (kind _BASE)


@dataclass
class _RenderInfo:
    tables: Set[str] = field(default_factory=set)
    columns: Set[str] = field(default_factory=set)
    sources: Set[Tuple[int, str]] = field(default_factory=set)
    sources_by_table: Dict[str, Set[Tuple[int, str]]] = field(default_factory=dict)
    outer: bool = False          # reads a column of an enclosing scope (correlated)
    bad: bool = False            # cannot be written over base tables (CTE reference, untraceable column)
    ambiguous: bool = False      # has an unqualified column of its own scope
    has_aggregate: bool = False  # aggregate outside subqueries (a group-level condition)
    has_parameter: bool = False
    has_subquery: bool = False


class _Resolver:
    """Traces a column to the base-table column it reads (see the module docstring)."""

    def __init__(self, schema: Mapping[str, FrozenSet[str]]) -> None:
        self.schema = schema
        self._transparent: Dict[int, bool] = {}

    def locate(self, scope: Scope, qualifier: str) -> Optional[Scope]:
        """The scope (this one or an enclosing one) whose FROM/JOIN defines ``qualifier``."""
        current: Optional[Scope] = scope
        while current is not None:
            if qualifier in current.selected_sources:
                return current
            current = current.parent
        return None

    def column(self, scope: Scope, column: exp.Column, depth: int = 0) -> _Resolution:
        ident = column.this
        if not isinstance(ident, exp.Identifier):
            return _Resolution(_AMBIGUOUS)
        qualifier = column.table
        if qualifier:
            owner = self.locate(scope, qualifier)
            if owner is not None:
                return self.source(owner, qualifier, ident, depth)
            parts = _qualifier_parts(column)   # not a source of any scope: take the qualifier as a table name
            if parts is None:
                return _Resolution(_UNRESOLVED, scope, qualifier)
            return _Resolution(_BASE, scope, qualifier, parts, _ident(ident))
        alias = self.only_source(scope, ident.name)
        if alias is None:
            return _Resolution(_AMBIGUOUS)
        return self.source(scope, alias, ident, depth)

    def only_source(self, scope: Scope, name: str) -> Optional[str]:
        """The source an unqualified column ``name`` belongs to, if it can be decided."""
        sources = scope.selected_sources
        if len(sources) == 1:
            return next(iter(sources))
        if len(sources) > 1 and self.schema:
            hits = [alias for alias in sources if self._provides(scope, alias, name)]
            if len(hits) == 1:
                return hits[0]
        return None

    def source(self, scope: Scope, alias: str, ident: exp.Identifier, depth: int) -> _Resolution:
        node, source = scope.selected_sources[alias]
        if isinstance(source, exp.Table):
            parts = _table_parts(source)
            if parts is None:
                return _Resolution(_UNRESOLVED, scope, alias)
            return _Resolution(_BASE, scope, alias, parts, _ident(ident))
        if isinstance(source, Scope) and depth < _MAX_DEPTH and self.transparent(source) and not _renames_columns(node):
            inner = self.output(source, ident, depth + 1)
            if inner.kind == _BASE:
                return _Resolution(_BASE, scope, alias, inner.table, inner.column)
        return _Resolution(_UNRESOLVED, scope, alias)

    def output(self, scope: Scope, ident: exp.Identifier, depth: int) -> _Resolution:
        """Resolve output column ``ident`` of a CTE or derived-table scope."""
        projections = list(scope.expression.expressions)
        renamed = _derived_column_names(scope)
        if renamed:
            if ident.name not in renamed or renamed.index(ident.name) >= len(projections):
                return _Resolution(_UNRESOLVED)
            return self._projection(scope, projections[renamed.index(ident.name)], depth)
        star: Optional[str] = None   # '' for a bare *, 't' for t.*
        for projection in projections:
            if isinstance(projection, exp.Star):
                star = '' if star is None else star
            elif isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
                star = projection.table if star is None else star
            elif projection.alias_or_name == ident.name:
                return self._projection(scope, projection, depth)
        if star is None:
            return _Resolution(_UNRESOLVED)
        alias = star or self.only_source(scope, ident.name)
        if alias and alias in scope.selected_sources:
            return self.source(scope, alias, ident, depth)
        return _Resolution(_UNRESOLVED)

    def _projection(self, scope: Scope, projection: exp.Expression, depth: int) -> _Resolution:
        target = projection.unalias()
        if isinstance(target, exp.Column) and not isinstance(target.this, exp.Star):
            return self.column(scope, target, depth)
        return _Resolution(_UNRESOLVED)

    def transparent(self, scope: Scope) -> bool:
        """True if reading the scope's rows equals reading its sources' rows (no grouping, limits, ...)."""
        key = id(scope)
        if key not in self._transparent:
            self._transparent[key] = _is_transparent(scope.expression)
        return self._transparent[key]

    def _provides(self, scope: Scope, alias: str, name: str) -> bool:
        node, source = scope.selected_sources[alias]
        if isinstance(source, exp.Table):
            parts = _table_parts(source)
            return parts is not None and name.lower() in self.schema.get(_schema_key(parts), frozenset())
        if isinstance(source, Scope) and isinstance(source.expression, exp.Select):
            return name in source.expression.named_selects
        return False


class _Analyzer:
    """Scope-aware feature extraction for one parsed query (see the module docstring)."""

    def __init__(self, root: exp.Expression, sql: str, dialect: Optional[str], query_id: Optional[str],
                 schema: Optional[Mapping[str, Any]]) -> None:
        self.root = root
        self.sql = sql
        self.dialect = dialect
        self.query_id = query_id
        self.resolver = _Resolver(_schema_index(schema))
        self.scopes: List[Scope] = []
        self.by_id: Dict[int, Scope] = {}
        self.ancestors: Dict[int, FrozenSet[int]] = {}
        self.qualifiers: Dict[int, Dict[str, Ident]] = {}
        self.columns_by_table: Dict[str, Set[str]] = {}
        self.ambiguous = False

    def run(self) -> QueryAnalysis:
        root = normalize_identifiers(self.root, dialect=self.dialect)
        _unquote_plain_identifiers(root, self.dialect)
        _expand_select_references(root)
        self.root = root
        self.scopes = traverse_scope(root)
        self.by_id = {id(s): s for s in self.scopes}
        self.ancestors = {id(s): _ancestor_ids(s) for s in self.scopes}
        self._annotate_sources()
        self._annotate_columns()
        analysis = self._extract()   # fragments are rendered from copies of the annotated tree ...
        tree = self._normalize_in_place()   # ... so the tree itself can then become the normalized query
        features = analysis.features
        features.normalized = tree.sql(dialect=self.dialect, pretty=True, comments=False)
        features.skeleton = _skeleton_in_place(tree, self.dialect)
        features.query_id = self.query_id or _default_query_id(features.normalized)
        return analysis

    # ------------------------------------------------------------------ annotation
    def _annotate_sources(self) -> None:
        """Choose the qualifier every FROM/JOIN source gets in normalized SQL (table name where unambiguous)."""
        base: Dict[int, List[Tuple[str, exp.Table, TableParts]]] = {}   # tables and CTE references
        scopes_of_table: Dict[TableParts, Set[int]] = {}
        for scope in self.scopes:
            for alias, (node, source) in scope.selected_sources.items():
                if not isinstance(node, exp.Table) or not isinstance(source, (exp.Table, Scope)):
                    continue
                if isinstance(source, Scope):
                    node.meta[_CTE_META] = True
                parts = _table_parts(node)
                if parts is not None:
                    base.setdefault(id(scope), []).append((alias, node, parts))
                    scopes_of_table.setdefault(parts, set()).add(id(scope))
        for scope in self.scopes:
            qualifiers = {alias: _source_name(node, alias) for alias, (node, _) in scope.selected_sources.items()}
            mine = base.get(id(scope), [])
            counts = Counter(parts for _, _, parts in mine)
            serial: Counter = Counter()
            taken = set(qualifiers)
            for alias, node, parts in mine:
                table_alias = node.args.get('alias')
                if len(scopes_of_table[parts]) > 1 or (isinstance(table_alias, exp.TableAlias) and table_alias.columns):
                    continue   # keep aliases of tables used in several scopes, and aliases that rename columns
                if counts[parts] == 1:
                    name, action = parts[-1], ('drop',)
                else:
                    serial[parts] += 1
                    renamed = exp.to_identifier(f'{parts[-1][0]}_{serial[parts]}')
                    name = (renamed.name, bool(renamed.quoted))
                    action = ('rename',) + name
                if name[0] != alias and name[0] in taken:
                    continue
                taken.discard(alias)
                taken.add(name[0])
                node.meta[_ALIAS_META] = action
                qualifiers[alias] = name
            self.qualifiers[id(scope)] = qualifiers

    def _annotate_columns(self) -> None:
        for scope in self.scopes:
            for column in scope.find_all(exp.Column):
                column.meta[_COLUMN_META] = self._column_meta(scope, column)

    def _column_meta(self, scope: Scope, column: exp.Column) -> Dict[str, Any]:
        meta: Dict[str, Any] = {'scope': id(scope)}
        if isinstance(column.this, exp.Star):
            owner = self.resolver.locate(scope, column.table) if column.table else None
            meta.update(kind=_STAR, norm=self.qualifiers[id(owner)].get(column.table) if owner else None)
            return meta
        result = self.resolver.column(scope, column)
        meta['kind'] = result.kind
        if result.src_scope is not None:
            meta.update(src_scope=id(result.src_scope), src_alias=result.src_alias,
                        norm=self.qualifiers.get(id(result.src_scope), {}).get(result.src_alias))
        if result.kind == _BASE:
            meta.update(table=result.table, column=result.column)
            self.columns_by_table.setdefault(_table_sql(result.table, self.dialect), set()).add(result.column[0])
        elif result.kind == _AMBIGUOUS:
            self.ambiguous = True
        return meta

    # ------------------------------------------------------------------ normalized SQL
    def _normalize_in_place(self) -> exp.Expression:
        """Rewrite the annotated tree into the normalized query (call after the fragments are extracted)."""
        tree = self.root
        for column in list(tree.find_all(exp.Column)):
            norm = (column.meta.get(_COLUMN_META) or {}).get('norm')
            if norm is not None:
                _qualify(column, norm)
        for table in tree.find_all(exp.Table):
            _apply_alias(table)
        for select in list(tree.find_all(exp.Select)):
            _canonicalize_conditions(select, self.dialect)
        for join in tree.find_all(exp.Join):
            _drop_redundant_join_keywords(join)
        return tree

    # ------------------------------------------------------------------ fragments
    def _extract(self) -> QueryAnalysis:
        """All fragments of all scopes (``normalized``, ``skeleton`` and the default id are filled in by run)."""
        predicates: Dict[str, Fragment] = {}
        filters: Dict[str, Fragment] = {}
        aggregates: Dict[str, Fragment] = {}
        measures: Dict[str, Fragment] = {}
        joins: Dict[str, Fragment] = {}
        tables: Set[str] = set()
        group_by: Dict[int, List[str]] = {}
        own_conditions: Dict[int, Dict[str, Fragment]] = {}
        scope_measures: Dict[int, Dict[str, Fragment]] = {}
        for scope in self.scopes:
            tables |= self._base_tables(scope)
            select = scope.expression
            if not isinstance(select, exp.Select):
                continue
            conditions = own_conditions.setdefault(id(scope), {})
            for node, origin in _conjuncts_of(select):
                join = self._join(node)
                if join is not None:
                    _merge(joins, join)
                    continue
                if origin == 'outer_on':
                    continue
                fragment, info = self._render(node, scope)
                if info.bad or info.outer or not (info.columns or info.ambiguous or info.has_subquery
                                                  or info.has_aggregate):
                    continue   # correlated, untraceable or constant (1 = 1)
                if origin == 'inner_on' and len(info.sources) > 1:
                    continue   # a non-equality join condition, not a filter
                if info.ambiguous or (info.has_aggregate and not fragment.tables):   # e.g. HAVING COUNT(*) > 5
                    self._widen_to_scope(fragment, scope)
                _merge(predicates, fragment)
                if not info.has_aggregate and not info.has_parameter:
                    _merge(filters, fragment)
                    _merge(conditions, fragment)
            for join in self._using_joins(scope, select):
                _merge(joins, join)
            for node in _aggregate_nodes(scope):
                rendered = self._aggregate_fragment(node, scope)
                if rendered is None:
                    continue
                fragment, info = rendered
                _merge(aggregates, fragment)
                if not info.has_parameter:
                    _merge(measures, fragment)
                    _merge(scope_measures.setdefault(id(scope), {}), fragment)
            group_by[id(scope)] = self._group_by(scope, select)
        features = QueryFeatures(
            query_id=self.query_id or '', sql=self.sql.strip(), normalized='', skeleton='', tables=sorted(tables),
            aggregates=_sorted(aggregates), predicates=_sorted(predicates), joins=_sorted(joins),
            group_by=_ordered_unique(g for s in reversed(self.scopes) for g in group_by.get(id(s), [])))
        return QueryAnalysis(features=features, measures=_sorted(measures), filters=_sorted(filters),
                             metric_uses=self._metric_uses(own_conditions, scope_measures),
                             columns_by_table={t: sorted(c) for t, c in sorted(self.columns_by_table.items())},
                             ambiguous=self.ambiguous)

    def _base_tables(self, scope: Scope) -> Set[str]:
        out = set()
        for _, (_, source) in scope.selected_sources.items():
            parts = _table_parts(source) if isinstance(source, exp.Table) else None
            if parts is not None:
                out.add(_table_sql(parts, self.dialect))
        return out

    def _aggregate_fragment(self, node: exp.Expression, scope: Scope) -> Optional[Tuple[Fragment, _RenderInfo]]:
        fragment, info = self._render(node, scope)
        if info.bad or info.outer:
            return None
        if (not info.tables or info.ambiguous) and not self._widen_to_scope(fragment, scope):
            return None   # COUNT(*) or unqualified columns over rows that cannot be traced to base tables
        return (fragment, info) if fragment.tables else None

    def _widen_to_scope(self, fragment: Fragment, scope: Scope) -> bool:
        """Add the tables of the whole scope (for COUNT(*) and unqualified columns); False if unknown."""
        scope_tables = self._scope_tables(scope)
        if scope_tables is None:
            return False
        fragment.tables = sorted(set(fragment.tables) | scope_tables)
        return True

    def _group_by(self, scope: Scope, select: exp.Select) -> List[str]:
        group = select.args.get('group')
        out: List[str] = []
        for node in (group.expressions if group is not None else []):
            fragment, info = self._render(node, scope)
            if not (info.bad or info.outer) and (info.columns or info.ambiguous):
                out.append(fragment.sql)
        return out

    def _render(self, node: exp.Expression, scope: Scope) -> Tuple[Fragment, _RenderInfo]:
        """Canonical fragment text of ``node`` written over base tables, plus what it reads."""
        sid = id(scope)
        info = _RenderInfo()
        tree = node.copy()
        for column in list(tree.find_all(exp.Column)):
            replacement = self._render_column(column, sid, info)
            if replacement is not None:
                if column is tree:
                    tree = replacement
                else:
                    column.replace(replacement)
        inner_ctes = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
        for table in tree.find_all(exp.Table):
            # a CTE defined outside the fragment cannot be referenced from it
            info.bad = info.bad or (bool(table.meta.get(_CTE_META)) and table.name not in inner_ctes)
            _apply_alias(table)
        # two instances of one table (self-join roles such as c.region = c2.region) cannot be told apart
        info.bad = info.bad or any(len(s) > 1 for s in info.sources_by_table.values())
        info.has_parameter = tree.find(exp.Placeholder, exp.Parameter) is not None
        info.has_subquery = tree.find(exp.Select) is not None
        info.has_aggregate = any(isinstance(n, exp.AggFunc) for n in _walk_local(tree))
        canonical = _strip_parens(_canonical(tree, self.dialect))
        sql = canonical.sql(dialect=self.dialect, comments=False)
        return Fragment(sql=sql, tables=sorted(info.tables), columns=sorted(info.columns)), info

    def _render_column(self, column: exp.Column, sid: int, info: _RenderInfo) -> Optional[exp.Expression]:
        """Rewrite one column of a fragment copy; returns a replacement node or None (rewritten in place)."""
        meta = column.meta.get(_COLUMN_META)
        if meta is None:
            info.ambiguous = True
            return None
        kind, norm, src = meta['kind'], meta.get('norm'), meta.get('src_scope')
        if kind == _STAR:
            if norm is not None:
                _qualify(column, norm)
            return None
        if kind == _AMBIGUOUS:
            info.ambiguous = info.ambiguous or meta['scope'] == sid
            return None
        local, outer = src == sid, src is not None and src in self.ancestors[sid]
        if not local and not outer:
            # the column belongs to a scope nested in the fragment (a subquery): keep that subquery valid SQL
            if norm is not None:
                _qualify(column, norm)
            if kind == _BASE:
                info.columns.add(_column_sql(meta['table'], meta['column'], self.dialect))
            return None
        if kind != _BASE:
            info.bad = True
            return None
        info.columns.add(_column_sql(meta['table'], meta['column'], self.dialect))
        if local:
            table = _table_sql(meta['table'], self.dialect)
            info.tables.add(table)
            info.sources.add((src, meta['src_alias']))
            info.sources_by_table.setdefault(table, set()).add((src, meta['src_alias']))
        else:
            info.outer = True
        if meta['scope'] != sid and self._shadowed(meta, sid):
            info.bad = True
        return _base_column(meta['table'], meta['column'])

    def _shadowed(self, meta: Mapping[str, Any], sid: int) -> bool:
        """True if a subquery between the column and the fragment's scope reuses the column's table name."""
        bare = meta['table'][-1][0]
        scope = self.by_id.get(meta['scope'])
        while scope is not None and id(scope) != sid:
            if bare in {name for name, _ in self.qualifiers.get(id(scope), {}).values()}:
                return True
            scope = scope.parent
        return False

    def _join(self, node: exp.Expression) -> Optional[Fragment]:
        """A join fragment if ``node`` equates plain columns of two different sources."""
        if not isinstance(node, exp.EQ):
            return None
        left, right = _strip_parens(node.this), _strip_parens(node.expression)
        if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
            return None
        lm, rm = left.meta.get(_COLUMN_META) or {}, right.meta.get(_COLUMN_META) or {}
        if lm.get('kind') != _BASE or rm.get('kind') != _BASE:
            return None
        if (lm['src_scope'], lm['src_alias']) == (rm['src_scope'], rm['src_alias']):
            return None
        return self._join_fragment((lm['table'], lm['column']), (rm['table'], rm['column']))

    def _join_fragment(self, a: Tuple[TableParts, Ident], b: Tuple[TableParts, Ident]) -> Optional[Fragment]:
        first, second = _base_column(*a), _base_column(*b)
        first_sql, second_sql = first.sql(dialect=self.dialect), second.sql(dialect=self.dialect)
        if first_sql == second_sql:
            return None
        if second_sql < first_sql:
            first, second, first_sql, second_sql = second, first, second_sql, first_sql
        return Fragment(sql=exp.EQ(this=first, expression=second).sql(dialect=self.dialect),
                        tables=sorted({_table_sql(a[0], self.dialect), _table_sql(b[0], self.dialect)}),
                        columns=sorted({first_sql, second_sql}))

    def _using_joins(self, scope: Scope, select: exp.Select) -> List[Fragment]:
        """Join fragments for ``JOIN t USING (k)``: the previous source's k equals t's k."""
        from_ = _arg(select, 'from')
        if from_ is None:
            return []
        previous, out = from_.this, []
        for join in select.args.get('joins') or []:
            left, right = previous.alias_or_name, join.this.alias_or_name
            previous = join.this
            if not join.args.get('using') or left not in scope.selected_sources or right not in scope.selected_sources:
                continue
            for item in join.args['using']:
                ident = item if isinstance(item, exp.Identifier) else item.this
                if not isinstance(ident, exp.Identifier):
                    continue
                a = self.resolver.source(scope, left, ident, 0)
                b = self.resolver.source(scope, right, ident, 0)
                if a.kind == _BASE and b.kind == _BASE:
                    fragment = self._join_fragment((a.table, a.column), (b.table, b.column))
                    if fragment is not None:
                        out.append(fragment)
        return out

    def _scope_tables(self, scope: Scope, depth: int = 0) -> Optional[Set[str]]:
        """Base tables whose rows a scope reads (through transparent derived tables), or None if unknown."""
        out: Set[str] = set()
        for _, (_, source) in scope.selected_sources.items():
            parts = _table_parts(source) if isinstance(source, exp.Table) else None
            if parts is not None:
                out.add(_table_sql(parts, self.dialect))
            elif isinstance(source, Scope) and depth < _MAX_DEPTH and self.resolver.transparent(source):
                inner = self._scope_tables(source, depth + 1)
                if inner is None:
                    return None
                out |= inner
            else:
                return None
        return out

    def _metric_uses(self, own: Dict[int, Dict[str, Fragment]],
                     measures: Dict[int, Dict[str, Fragment]]) -> List[MetricUse]:
        """Each aggregate with the conditions of its scope and of the inner-joined CTEs/derived tables it reads."""
        cache: Dict[int, Dict[str, Fragment]] = {}

        def applicable(scope: Scope, depth: int) -> Dict[str, Fragment]:
            key = id(scope)
            if key not in cache:
                cache[key] = {}
                result = dict(own.get(key, {}))
                select = scope.expression
                if isinstance(select, exp.Select) and depth < _MAX_DEPTH and not _has_right_or_full_join(select):
                    for _, (node, source) in scope.selected_sources.items():
                        if isinstance(source, Scope) and self.resolver.transparent(source) and _inner_source(node, select):
                            for fragment in applicable(source, depth + 1).values():
                                _merge(result, fragment)
                cache[key] = result
            return cache[key]

        uses: Dict[Tuple[str, Tuple[str, ...]], MetricUse] = {}
        for scope in self.scopes:
            for aggregate in measures.get(id(scope), {}).values():
                conditions = applicable(scope, 0)
                use = MetricUse(aggregate=aggregate, conditions=[conditions[k] for k in sorted(conditions)])
                uses.setdefault((aggregate.sql, tuple(sorted(conditions))), use)
        return [uses[k] for k in sorted(uses)]


# ---------------------------------------------------------------------------------------------- helpers
def _arg(node: exp.Expression, key: str) -> Any:
    """``node.args[key]``; sqlglot 30 spells reserved-word keys with a trailing underscore ('from_')."""
    value = node.args.get(f'{key}_')
    return value if value is not None else node.args.get(key)


def _ident(identifier: exp.Identifier) -> Ident:
    return (identifier.name, bool(identifier.quoted))


def _identifier(ident: Ident) -> exp.Identifier:
    return exp.Identifier(this=ident[0], quoted=ident[1])


def _table_parts(table: exp.Table) -> Optional[TableParts]:
    """Identifiers of a plain table reference, or None for table functions and other sources."""
    parts = []
    for key in ('catalog', 'db', 'this'):
        value = table.args.get(key)
        if value is None:
            continue
        if not isinstance(value, exp.Identifier):
            return None
        parts.append(_ident(value))
    return tuple(parts) or None


def _qualifier_parts(column: exp.Column) -> Optional[TableParts]:
    parts = []
    for key in ('catalog', 'db', 'table'):
        value = column.args.get(key)
        if value is None:
            continue
        if not isinstance(value, exp.Identifier):
            return None
        parts.append(_ident(value))
    return tuple(parts) or None


@lru_cache(maxsize=16384)
def _table_sql(parts: TableParts, dialect: Optional[str]) -> str:
    idents = [_identifier(p) for p in parts]
    kwargs: Dict[str, Any] = {'this': idents[-1]}
    if len(idents) > 1:
        kwargs['db'] = idents[-2]
    if len(idents) > 2:
        kwargs['catalog'] = idents[-3]
    return exp.Table(**kwargs).sql(dialect=dialect)


def _base_column(parts: TableParts, column: Ident) -> exp.Column:
    idents = [_identifier(p) for p in parts]
    kwargs: Dict[str, Any] = {'this': _identifier(column), 'table': idents[-1]}
    if len(idents) > 1:
        kwargs['db'] = idents[-2]
    if len(idents) > 2:
        kwargs['catalog'] = idents[-3]
    return exp.Column(**kwargs)


@lru_cache(maxsize=65536)
def _column_sql(parts: TableParts, column: Ident, dialect: Optional[str]) -> str:
    return _base_column(parts, column).sql(dialect=dialect)


def _schema_key(parts: TableParts) -> str:
    return '.'.join(name.lower() for name, _ in parts)


def _schema_index(schema: Optional[Mapping[str, Any]]) -> Dict[str, FrozenSet[str]]:
    """Lower-cased ``table -> columns`` lookup; table keys may be written with or without quotes."""
    index: Dict[str, FrozenSet[str]] = {}
    for table, columns in (schema or {}).items():
        key = re.sub(r'["`\[\]]', '', str(table)).strip().lower()
        index[key] = index.get(key, frozenset()) | frozenset(str(c).lower() for c in columns)
    return index


def _source_name(node: exp.Expression, alias: str) -> Ident:
    """The identifier under which a FROM/JOIN source is referenced in its scope."""
    holder = node.parent if isinstance(node, _QUERY_NODES) and isinstance(node.parent, exp.Subquery) else node
    table_alias = holder.args.get('alias') if holder is not None else None
    if isinstance(table_alias, exp.TableAlias) and isinstance(table_alias.this, exp.Identifier):
        return _ident(table_alias.this)
    if isinstance(node, exp.Table) and isinstance(node.this, exp.Identifier):
        return _ident(node.this)
    return (alias, False)


def _renames_columns(node: exp.Expression) -> bool:
    table_alias = node.args.get('alias') if isinstance(node, exp.Table) else None
    return isinstance(table_alias, exp.TableAlias) and bool(table_alias.columns)


def _derived_column_names(scope: Scope) -> List[str]:
    """Column names given by ``WITH t(a, b) AS (...)`` or ``(...) AS t(a, b)``."""
    holder = scope.expression.parent
    if isinstance(holder, (exp.CTE, exp.Subquery)):
        table_alias = holder.args.get('alias')
        if isinstance(table_alias, exp.TableAlias) and table_alias.columns:
            return [c.name for c in table_alias.columns]
    return []


def _is_transparent(select: exp.Expression) -> bool:
    if not isinstance(select, exp.Select):
        return False
    if any(select.args.get(key) for key in ('group', 'having', 'qualify', 'limit', 'offset', 'distinct')):
        return False
    return not any(isinstance(n, (exp.AggFunc, exp.Window)) for p in select.expressions for n in _walk_local(p))


def _ancestor_ids(scope: Scope) -> FrozenSet[int]:
    out = set()
    parent = scope.parent
    while parent is not None:
        out.add(id(parent))
        parent = parent.parent
    return frozenset(out)


def _walk_local(node: exp.Expression) -> Iterator[exp.Expression]:
    """``node`` and its descendants, without entering nested queries."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(child for child in current.iter_expressions() if not isinstance(child, _QUERY_NODES))


def _qualify(column: exp.Column, qualifier: Ident) -> None:
    column.set('table', _identifier(qualifier))
    column.set('db', None)
    column.set('catalog', None)


def _apply_alias(table: exp.Table) -> None:
    action = table.meta.get(_ALIAS_META)
    if not action:
        return
    if action[0] == 'drop':
        table.set('alias', None)
    else:
        table.set('alias', exp.TableAlias(this=exp.Identifier(this=action[1], quoted=action[2])))


def _drop_redundant_join_keywords(join: exp.Join) -> None:
    kind = str(join.args.get('kind') or '').upper()
    if kind == 'INNER' or (kind == 'OUTER' and join.args.get('side')):
        join.set('kind', None)


def _is_inner(join: exp.Join) -> bool:
    side = str(join.args.get('side') or '').upper()
    kind = str(join.args.get('kind') or '').upper()
    return not side and kind in ('', 'INNER', 'CROSS')


def _has_right_or_full_join(select: exp.Select) -> bool:
    return any(str(j.args.get('side') or '').upper() in ('RIGHT', 'FULL') for j in select.args.get('joins') or [])


def _inner_source(node: exp.Expression, select: exp.Select) -> bool:
    """True if a FROM/JOIN source is in FROM or inner-joined (its rows are not padded with NULLs)."""
    current: Optional[exp.Expression] = node
    while current is not None and current is not select:
        if isinstance(current, exp.Join):
            return _is_inner(current)
        if isinstance(current, exp.From):
            return True
        current = current.parent
    return False


def _conjuncts(node: Optional[exp.Expression]) -> List[exp.Expression]:
    """Top-level AND operands of a condition, without parentheses (OR groups stay whole)."""
    out: List[exp.Expression] = []
    stack = [node] if node is not None else []
    while stack:
        current = _strip_parens(stack.pop())
        if isinstance(current, exp.And):
            stack.extend([current.expression, current.this])
        else:
            out.append(current)
    return out


def _conjuncts_of(select: exp.Select) -> List[Tuple[exp.Expression, str]]:
    """Conjuncts of WHERE, of each JOIN ... ON (tagged inner/outer) and of HAVING."""
    out = []
    where = select.args.get('where')
    if where is not None:
        out += [(n, 'where') for n in _conjuncts(where.this)]
    nullable = _has_right_or_full_join(select)
    for join in select.args.get('joins') or []:
        on = join.args.get('on')
        if on is not None:
            origin = 'inner_on' if _is_inner(join) and not nullable else 'outer_on'
            out += [(n, origin) for n in _conjuncts(on)]
    having = select.args.get('having')
    if having is not None:
        out += [(n, 'having') for n in _conjuncts(having.this)]
    return out


def _aggregate_nodes(scope: Scope) -> List[exp.Expression]:
    """Outermost SUM/COUNT/AVG/MIN/MAX calls of a scope (with their FILTER clause, if any)."""
    out = []
    for node in scope.find_all(*AGGREGATE_TYPES):
        if isinstance(node, (exp.Max, exp.Min)) and node.expressions:
            continue   # scalar MAX(a, b) / MIN(a, b)
        parent = node.parent
        nested = False
        while parent is not None and parent is not scope.expression:
            nested = nested or isinstance(parent, exp.AggFunc)
            parent = parent.parent
        if not nested:
            out.append(node.parent if isinstance(node.parent, exp.Filter) and node.parent.this is node else node)
    return out


def _expand_select_references(root: exp.Expression) -> None:
    """Replace positional GROUP BY items and select-alias references in GROUP BY/HAVING/ORDER BY."""
    for select in list(root.find_all(exp.Select)):
        projections = list(select.expressions)
        group = select.args.get('group')
        for item in list(group.expressions) if group is not None else []:
            if isinstance(item, exp.Literal) and not item.is_string and str(item.this).isdigit():
                index = int(item.this)
                if 1 <= index <= len(projections):
                    target = projections[index - 1].unalias()
                    if not isinstance(target, exp.Star) and not (isinstance(target, exp.Column) and isinstance(target.this, exp.Star)):
                        item.replace(target.copy())
        named = {}
        for projection in projections:
            if isinstance(projection, exp.Alias) and projection.alias:
                named.setdefault(projection.alias, projection)
        for key in ('group', 'having', 'order') if named else ():
            clause = select.args.get(key)
            for column in [n for n in _walk_local(clause) if isinstance(n, exp.Column)] if clause is not None else []:
                if column.table or column.name not in named:
                    continue
                target = named[column.name].this
                if not (isinstance(target, exp.Column) and target.name == column.name):
                    column.replace(target.copy())


def _unquote_plain_identifiers(root: exp.Expression, dialect: Optional[str]) -> None:
    for identifier in root.find_all(exp.Identifier):
        if identifier.quoted and _is_plain_name(identifier.name, dialect):
            identifier.set('quoted', False)


@lru_cache(maxsize=8192)
def _is_plain_name(name: str, dialect: Optional[str]) -> bool:
    """True if ``name`` means the same quoted and unquoted in ``dialect`` (lower-case, not a keyword)."""
    if not _PLAIN_NAME.match(name) or name.upper() in _reserved_words(dialect):
        return False
    return Dialect.get_or_raise(dialect).normalize_identifier(exp.to_identifier(name)).name == name


@lru_cache(maxsize=64)
def _reserved_words(dialect: Optional[str]) -> FrozenSet[str]:
    keywords = Dialect.get_or_raise(dialect).tokenizer_class.KEYWORDS
    return frozenset(word for key in keywords for word in key.split()) | _SQL_RESERVED


def _canonicalize_conditions(select: exp.Select, dialect: Optional[str]) -> None:
    for key in ('where', 'having'):
        clause = select.args.get(key)
        if clause is not None and clause.this is not None:
            clause.set('this', _strip_parens(_canonical(clause.this, dialect)))
    for join in select.args.get('joins') or []:
        on = join.args.get('on')
        if on is not None:
            join.set('on', _strip_parens(_canonical(on, dialect)))


def _canonical(node: exp.Expression, dialect: Optional[str]) -> exp.Expression:
    """Canonical form of an expression tree (bottom-up); may rewrite ``node`` in place, returns the new root."""
    if isinstance(node, (exp.And, exp.Or)):
        operands: Dict[str, exp.Expression] = {}
        for operand in _flatten(node, type(node)):
            operand = _strip_parens(_canonical(operand, dialect))
            operands.setdefault(_text(operand, dialect), operand)
        return _connect(type(node), [operands[k] for k in sorted(operands)])
    for child in list(node.iter_expressions()):
        new = _canonical(child, dialect)
        if new is not child:
            child.replace(new)
    return _canonical_node(node, dialect)


def _canonical_node(node: exp.Expression, dialect: Optional[str]) -> exp.Expression:
    if isinstance(node, exp.Paren):
        return node.this if isinstance(node.this, _ATOMS) else node
    if isinstance(node, exp.Not):
        inner = node.this
        while isinstance(inner, exp.Paren) and not isinstance(inner.this, exp.Connector):
            inner = inner.this
        if isinstance(inner, (exp.Like, exp.ILike)) and 'negate' in inner.arg_types and not inner.args.get('negate'):
            inner.set('negate', True)   # sqlglot >= 30.8; older versions would drop the NOT, so keep Not(Like)
            return inner
        if isinstance(inner, exp.Not):
            return inner.this
        if inner is not node.this:
            node.set('this', inner)
        return node
    mirrored = _MIRRORED.get(type(node))
    if mirrored is not None:
        left, right = node.this, node.expression
        left_column, right_column = _has_local_column(left), _has_local_column(right)
        if (right_column and not left_column) or (left_column and right_column and _text(right, dialect) < _text(left, dialect)):
            return mirrored(this=right, expression=left)
        return node
    if isinstance(node, exp.In) and not node.args.get('query') and node.expressions \
            and all(_is_constant(e) for e in node.expressions):
        values: Dict[str, exp.Expression] = {}
        for value in node.expressions:
            values.setdefault(_text(value, dialect), value)
        node.set('expressions', [values[k] for k in sorted(values)])
        return node
    if isinstance(node, exp.Count) and isinstance(node.this, exp.Literal):
        node.set('this', exp.Star())
    return node


def _flatten(node: exp.Expression, kind: type) -> List[exp.Expression]:
    out: List[exp.Expression] = []
    stack = [node]
    while stack:
        current = stack.pop()
        while isinstance(current, exp.Paren) and isinstance(current.this, kind):
            current = current.this
        if isinstance(current, kind):
            stack.extend([current.expression, current.this])
        else:
            out.append(current)
    return out


def _connect(kind: type, operands: List[exp.Expression]) -> exp.Expression:
    def wrap(operand: exp.Expression) -> exp.Expression:
        return exp.Paren(this=operand) if isinstance(operand, exp.Connector) else operand

    node = wrap(operands[0])
    for operand in operands[1:]:
        node = kind(this=node, expression=wrap(operand))
    return node


def _strip_parens(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _has_local_column(node: exp.Expression) -> bool:
    return any(isinstance(n, exp.Column) for n in _walk_local(node))


def _is_constant(node: exp.Expression) -> bool:
    return isinstance(node, _CONSTANTS) or (isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal))


def _text(node: exp.Expression, dialect: Optional[str]) -> str:
    return node.sql(dialect=dialect, comments=False)


def _skeleton_in_place(tree: exp.Expression, dialect: Optional[str]) -> str:
    """``tree`` with literals replaced by placeholders (modifies ``tree``), pretty-printed."""
    for literal in list(tree.find_all(exp.Literal, exp.Boolean)):
        literal.replace(exp.Placeholder())
    for node in tree.find_all(exp.In):
        if len(node.expressions) > 1 and all(isinstance(v, exp.Placeholder) for v in node.expressions):
            node.set('expressions', [exp.Placeholder()])
    return tree.sql(dialect=dialect, pretty=True, comments=False)


def _merge(store: Dict[str, Fragment], fragment: Fragment) -> None:
    """Add ``fragment`` to ``store`` (keyed by its SQL), uniting tables and columns of equal fragments."""
    current = store.get(fragment.sql)
    if current is None:
        store[fragment.sql] = Fragment(fragment.sql, list(fragment.tables), list(fragment.columns))
    else:
        store[fragment.sql] = Fragment(fragment.sql, sorted(set(current.tables) | set(fragment.tables)),
                                       sorted(set(current.columns) | set(fragment.columns)))


def _sorted(store: Mapping[str, Fragment]) -> List[Fragment]:
    return [store[k] for k in sorted(store)]


def _ordered_unique(items: Iterator[str]) -> List[str]:
    out: List[str] = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def _default_query_id(normalized: str) -> str:
    return 'q_' + hashlib.sha1(normalized.encode('utf-8')).hexdigest()[:10]
