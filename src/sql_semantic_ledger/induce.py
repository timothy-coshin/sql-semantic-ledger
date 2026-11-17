"""Generation (induce): business definitions from SQL usage histories and code.

CONTRACT (implemented by the build step):

    load_history(path) -> list[QueryRecord]
        '.sql': statements separated by ';' (ids q1, q2, ...); '.jsonl': one object per line with
        keys sql (required), id, user, at.
    induce(records, dialect=None, min_support=2, ledger=None, metrics=True, keep_unsupported=False) -> Ledger
        The plan's steps: (1) SQL normalization, (2) candidate generation, (3) independent-support check,
        (5) grade assignment. Step (4), falsification against data, is checks.run_checks.
        Candidates: MEASURE (aggregates), FILTER (predicates), JOIN (join conditions) and, if ``metrics``,
        METRIC = an aggregate together with the predicates that co-occur with it in a query
        (expression = aggregate, conditions = sorted predicates).
        Support: each record that yields a candidate adds Evidence(kind=record.kind, ref=record.ref or id).
        Two records count as different only if their skeletons differ or their refs differ.
        Candidates with support < min_support are dropped unless keep_unsupported (then they stay E0).
        Grades are not stored; they follow from the evidence (see model.Definition.grade).
        Names: readable snake_case built from the fragment, e.g. 'sum_orders_amount',
        'orders_status_eq_paid', 'join_customers_orders', 'sum_orders_amount_where_orders_status_eq_paid'.

Additions: ``load_history`` also reads the optional jsonl keys ``kind`` ('history' or 'code') and ``ref``,
so that records written by a code scan can be loaded again; ``definition_id`` and ``definition_name``
are the id and name rules below, for modules that create definitions of their own.

How candidates become definitions
---------------------------------
* (1) Every record is analysed by :func:`parse.analyze_query` (equal SQL text only once). When a query has
  an unqualified column that its scope cannot attribute to one table, the history serves as a schema hint
  (the tables other queries qualify that column with) and the query is analysed again.
* (2) MEASURE: each aggregate. FILTER: each row-level predicate (WHERE conjuncts, single-table conjuncts of
  INNER JOIN ... ON, HAVING conjuncts without aggregates). JOIN: each equality join. METRIC: an aggregate with
  the row-level predicates that apply to the rows it aggregates: those of its own query scope and of the
  CTEs / derived tables it reads through inner joins (predicates inside an IN/EXISTS subquery belong to that
  subquery). Group-level conditions (HAVING with an aggregate) and conditions with parameter placeholders
  (``?``, ``:name``, ``$1``) do not become FILTERs or METRIC conditions, because a definition's conditions must
  be usable in a WHERE clause. An aggregate without conditions is only a MEASURE.
* One-off conditions: a query supports the METRIC with all its conditions and also the METRIC restricted to
  the conditions that are themselves supported (FILTER support >= ``min_support``). An ad-hoc slice such as
  ``schools.county = 'Alameda'`` in one query therefore does not hide a metric that several queries share.
* (3) Independent support: the records that yield a candidate are grouped by (record kind, skeleton, record
  ref); each group adds one Evidence(kind=record.kind, ref=record.ref or record.id). Re-running the same query
  (same skeleton, no explicit ref) counts once; two code sites (different refs) count twice; a history query
  and a code site are different sources (``Definition.sources``). The groups behind each version are kept in
  ``meta['support_keys']`` (version -> keys), so a query that is run again in a later history merged into the
  same ledger still counts once.
* Identity: ``Definition.id`` is :func:`model.make_id` of (kind, expression, conditions); for an expression
  that reads no column, such as COUNT(*), the tables are added to the key so that COUNT(*) over orders and
  COUNT(*) over customers stay different definitions.
* (5) Grades are not stored. A new candidate is added when its support reaches ``min_support`` (or always,
  with ``keep_unsupported``). Evidence for a definition that is already in ``ledger`` is always merged, so
  support accumulates over several calls; it is attached to the current version, and skipped when that
  version's expression or conditions differ (the definition was revised) or when its group (record kind,
  skeleton, record ref) already supports that version.
* Stored values: ``tables`` and ``columns`` are SQL text in the induction dialect (quoted only where needed,
  e.g. ``frpm."free meal count (k-12)"``); ``source`` is 'history', 'code' or 'code+history';
  ``meta['occurrences']`` counts every record of the adding call that used the candidate (repeats included);
  ``meta['support_keys']`` maps a version (as text) to the sorted keys ``'<kind>|<skeleton hash>|<ref>'`` of
  its support groups; an evidence ``detail`` is the normalized query on one line, with the record's user and
  time when known.
* Ids of ``.sql`` records are positions (q1, q2, ...), and support counts distinct evidence refs. When several
  histories are merged into one ledger, give the records unique ids (``.jsonl``); otherwise two different
  queries at the same position share a ref and count once (support is under-counted, never over-counted).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect

from .model import Definition, Evidence, EvidenceKind, Kind, Ledger, make_id
from .parse import QueryAnalysis, analyze_query, split_statements

logger = logging.getLogger(__name__)

#: Record kinds that count as usage evidence.
USAGE_KINDS = (EvidenceKind.HISTORY, EvidenceKind.CODE)
#: Longer names are cut and end with a short hash of the definition.
MAX_NAME_LENGTH = 80
#: ``Definition.meta`` key of the support groups behind each version (see the module docstring).
SUPPORT_KEYS = 'support_keys'
_DETAIL_SQL_LENGTH = 240
_KIND_ORDER = {Kind.MEASURE: 0, Kind.FILTER: 1, Kind.JOIN: 2, Kind.METRIC: 3, Kind.DIMENSION: 4}


@dataclass
class QueryRecord:
    """One SQL statement observed in a usage history or found in service code."""

    sql: str
    id: Optional[str] = None
    kind: EvidenceKind = EvidenceKind.HISTORY      # HISTORY or CODE
    ref: Optional[str] = None                      # e.g. 'q17' or 'src/api/orders.js:42'
    user: Optional[str] = None
    at: Optional[str] = None


# ---------------------------------------------------------------------------------------------- loading
def load_history(path: Union[str, Path]) -> List[QueryRecord]:
    """Read a usage history: ``.sql`` (statements separated by ``;``) or ``.jsonl`` (one object per line).

    ``.sql`` statements get ids q1, q2, ... in file order. A ``.jsonl`` object needs ``sql`` and may have
    ``id`` (default q<n>), ``user``, ``at``, ``kind`` ('history' or 'code') and ``ref``. Raises ValueError for
    another file type or a malformed jsonl line (the message names the line).
    """
    file = Path(path)
    suffix = file.suffix.lower()
    if suffix == '.sql':
        statements = split_statements(file.read_text(encoding='utf-8-sig'))
        return [QueryRecord(sql=sql, id=f'q{i}') for i, sql in enumerate(statements, start=1)]
    if suffix == '.jsonl':
        return _load_jsonl(file)
    raise ValueError(f"unsupported history file {file.name!r}: use '.sql' (statements separated by ';') or '.jsonl'")


def _load_jsonl(file: Path) -> List[QueryRecord]:
    records: List[QueryRecord] = []
    with file.open(encoding='utf-8-sig') as handle:
        for line_no, line in enumerate(handle, start=1):
            if line.strip():
                records.append(_record_from_json(line, f'{file}:{line_no}', len(records) + 1))
    return records


def _record_from_json(line: str, where: str, position: int) -> QueryRecord:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as error:
        raise ValueError(f'{where}: invalid JSON ({error.msg})') from None
    if not isinstance(obj, dict):
        raise ValueError(f'{where}: expected a JSON object')
    sql = obj.get('sql')
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError(f"{where}: missing or empty 'sql' string")
    try:
        kind = EvidenceKind(str(obj.get('kind') or 'history').lower())
    except ValueError:
        kind = None
    if kind not in USAGE_KINDS:
        raise ValueError(f"{where}: 'kind' must be 'history' or 'code', not {obj.get('kind')!r}")
    record_id = obj.get('id')
    return QueryRecord(sql=sql, id=f'q{position}' if record_id in (None, '') else str(record_id), kind=kind,
                       ref=_optional_str(obj.get('ref')), user=_optional_str(obj.get('user')),
                       at=_optional_str(obj.get('at')))


def _optional_str(value: Any) -> Optional[str]:
    return None if value in (None, '') else str(value)


# ---------------------------------------------------------------------------------------------- induction
@dataclass
class _Candidate:
    """A candidate definition and the records that use it (record index -> record and its analysis)."""

    id: str
    kind: Kind
    expression: str
    conditions: Tuple[str, ...]
    key_tables: Tuple[str, ...]
    dialect: Optional[str]
    tables: Set[str] = field(default_factory=set)
    columns: Set[str] = field(default_factory=set)
    uses: Dict[int, Tuple[QueryRecord, QueryAnalysis]] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return definition_name(self.kind, self.expression, self.conditions, self.key_tables, self.dialect)

    def supporting(self) -> List[Tuple[str, Evidence]]:
        """(support key, evidence): one per group of records with the same (kind, skeleton, ref)."""
        out: List[Tuple[str, Evidence]] = []
        seen: Set[str] = set()
        for record, analysis in self.uses.values():
            key = _support_key(record, analysis)
            if key not in seen:
                seen.add(key)
                out.append((key, Evidence(kind=record.kind, ref=record.ref or record.id,
                                          detail=_detail(record, analysis))))
        return out

    def definition(self, supporting: Optional[List[Tuple[str, Evidence]]] = None) -> Definition:
        pairs = self.supporting() if supporting is None else supporting
        kinds = sorted({record.kind.value for record, _ in self.uses.values()})
        return Definition(name=self.name, kind=self.kind, expression=self.expression,
                          conditions=list(self.conditions), tables=sorted(self.tables), columns=sorted(self.columns),
                          source='+'.join(kinds), evidence=[evidence for _, evidence in pairs],
                          meta={'occurrences': len(self.uses), SUPPORT_KEYS: {'1': sorted(k for k, _ in pairs)}},
                          id=self.id)


def _support_key(record: QueryRecord, analysis: QueryAnalysis) -> str:
    """Key of a record's support group: its kind, a hash of its skeleton and its explicit ref."""
    digest = hashlib.sha1(analysis.features.skeleton.encode('utf-8')).hexdigest()[:16]
    return f'{record.kind.value}|{digest}|{record.ref or ""}'


def induce(records: Iterable[Union[QueryRecord, str]], dialect: Optional[str] = None, min_support: int = 2,
           ledger: Optional[Ledger] = None, metrics: bool = True, keep_unsupported: bool = False) -> Ledger:
    """Induce MEASURE, FILTER, JOIN and METRIC definitions from usage records (see the module docstring).

    ``records`` are QueryRecord objects or plain SQL strings (ids q1, q2, ... by position). Records that do not
    parse, or are not SELECT/WITH queries, are skipped. Definitions are added to ``ledger`` (a new Ledger if
    None), which is returned. Raises ValueError if ``min_support`` < 1 or a record kind is not history/code.
    """
    if isinstance(min_support, bool) or not isinstance(min_support, int) or min_support < 1:
        raise ValueError(f'min_support must be an integer >= 1, not {min_support!r}')
    target = ledger if ledger is not None else Ledger()
    prepared = _prepare_records(records)
    analyses = _analyze_records(prepared, dialect)
    candidates = _candidates(prepared, analyses, dialect)
    if metrics:
        _add_metric_candidates(candidates, prepared, analyses, min_support, dialect)
    added = merged = 0
    for candidate in sorted(candidates.values(), key=lambda c: (_KIND_ORDER[c.kind], c.name, c.id)):
        supporting = candidate.supporting()
        definition = candidate.definition(supporting)
        if definition.id in target:
            merged += _merge_into(target, definition, supporting)
        elif keep_unsupported or definition.support >= min_support:
            target.add(definition)
            added += 1
    logger.info('induce: %d records, %d analysed, %d candidates, %d definitions added, %d merged',
                len(prepared), sum(a is not None for a in analyses), len(candidates), added, merged)
    return target


def _merge_into(ledger: Ledger, definition: Definition, supporting: Sequence[Tuple[str, Evidence]]) -> int:
    """Merge new support into the ledger's definition with the same id; 1 if it received new evidence.

    A support group that already backs the current version (same record kind, skeleton and explicit ref, as
    recorded in ``meta['support_keys']``) is skipped, so re-running a query in a later history counts once.
    """
    current = ledger.get(definition.id)
    if current.expression != definition.expression or sorted(current.conditions) != sorted(definition.conditions):
        logger.debug('not merging evidence into revised definition %s (%s)', current.id, current.name)
        return 0
    store = current.meta.get(SUPPORT_KEYS)
    store = dict(store) if isinstance(store, dict) else {}
    version = str(current.version)
    stored = store.get(version)
    known = {key for key in stored if isinstance(key, str)} if isinstance(stored, list) else set()
    fresh: List[Evidence] = []
    for key, evidence in supporting:
        if key not in known:
            known.add(key)
            evidence.version = current.version
            fresh.append(evidence)
    if not fresh:
        return 0
    store[version] = sorted(known)
    current.meta[SUPPORT_KEYS] = store
    definition.evidence = fresh
    ledger.add(definition)
    return 1


def _prepare_records(records: Iterable[Union[QueryRecord, str]]) -> List[QueryRecord]:
    """Copies of the records with an id (q<position> by default) and a usage kind."""
    out: List[QueryRecord] = []
    for position, item in enumerate(records, start=1):
        if isinstance(item, str):
            out.append(QueryRecord(sql=item, id=f'q{position}'))
            continue
        if not isinstance(item, QueryRecord):
            raise TypeError(f'records must be QueryRecord or str, not {type(item).__name__}')
        kind = item.kind if isinstance(item.kind, EvidenceKind) else EvidenceKind(item.kind)
        if kind not in USAGE_KINDS:
            raise ValueError(f'record {item.id or position}: kind must be history or code, not {kind.value!r}')
        out.append(dataclasses.replace(item, id=item.id or f'q{position}', kind=kind))
    return out


def _analyze_records(records: Sequence[QueryRecord], dialect: Optional[str]) -> List[Optional[QueryAnalysis]]:
    """Analyse each record (equal SQL text once); re-analyse ambiguous queries with the history's columns."""
    cache: Dict[str, Optional[QueryAnalysis]] = {}
    for record in records:
        if isinstance(record.sql, str) and record.sql not in cache:
            cache[record.sql] = analyze_query(record.sql, dialect=dialect, query_id=record.id)
    if any(a is not None and a.ambiguous for a in cache.values()):
        schema: Dict[str, Set[str]] = {}
        for analysis in cache.values():
            for table, columns in (analysis.columns_by_table if analysis is not None else {}).items():
                schema.setdefault(table, set()).update(columns)
        for sql, analysis in list(cache.items()):
            if analysis is not None and analysis.ambiguous:
                cache[sql] = analyze_query(sql, dialect=dialect, query_id=analysis.features.query_id,
                                           schema=schema) or analysis
    out = [cache.get(r.sql) if isinstance(r.sql, str) else None for r in records]
    for record, analysis in zip(records, out):
        if analysis is None:
            logger.debug('skipped record %s: not a parsable SELECT/WITH query', record.id)
    return out


def _candidates(records: Sequence[QueryRecord], analyses: Sequence[Optional[QueryAnalysis]],
                dialect: Optional[str]) -> Dict[str, _Candidate]:
    """MEASURE, FILTER and JOIN candidates with the records that use them."""
    candidates: Dict[str, _Candidate] = {}
    for index, (record, analysis) in enumerate(zip(records, analyses)):
        if analysis is None:
            continue
        for kind, fragments in ((Kind.MEASURE, analysis.measures), (Kind.FILTER, analysis.filters),
                                (Kind.JOIN, analysis.features.joins)):
            for fragment in fragments:
                _use(candidates, kind, fragment.sql, (), fragment.tables, fragment.tables, fragment.columns,
                     index, record, analysis, dialect)
    return candidates


def _add_metric_candidates(candidates: Dict[str, _Candidate], records: Sequence[QueryRecord],
                           analyses: Sequence[Optional[QueryAnalysis]], min_support: int,
                           dialect: Optional[str]) -> None:
    """METRIC candidates: each aggregate with all its conditions and with its supported conditions."""
    filter_support = {c.expression: c.definition().support for c in candidates.values() if c.kind is Kind.FILTER}
    for index, (record, analysis) in enumerate(zip(records, analyses)):
        if analysis is None:
            continue
        for use in analysis.metric_uses:
            by_sql = {c.sql: c for c in use.conditions}
            every = tuple(sorted(by_sql))
            supported = tuple(c for c in every if filter_support.get(c, 0) >= min_support)
            for conditions in sorted({every, supported}):
                if not conditions:
                    continue
                tables = set(use.aggregate.tables).union(*(by_sql[c].tables for c in conditions))
                columns = set(use.aggregate.columns).union(*(by_sql[c].columns for c in conditions))
                _use(candidates, Kind.METRIC, use.aggregate.sql, conditions, use.aggregate.tables, tables, columns,
                     index, record, analysis, dialect)


def _use(candidates: Dict[str, _Candidate], kind: Kind, expression: str, conditions: Sequence[str],
         key_tables: Sequence[str], tables: Iterable[str], columns: Iterable[str], index: int,
         record: QueryRecord, analysis: QueryAnalysis, dialect: Optional[str]) -> None:
    """Record that ``record`` (position ``index``) uses the candidate, creating the candidate if needed."""
    definition_key = definition_id(kind, expression, conditions, key_tables, dialect)
    candidate = candidates.get(definition_key)
    if candidate is None:
        candidate = candidates[definition_key] = _Candidate(definition_key, kind, expression, tuple(conditions),
                                                            tuple(sorted(key_tables)), dialect)
    candidate.tables.update(tables)
    candidate.columns.update(columns)
    candidate.uses.setdefault(index, (record, analysis))


def _detail(record: QueryRecord, analysis: QueryAnalysis) -> str:
    """Evidence detail: the normalized query on one line, with the record's user and time if known."""
    sql = ' '.join(analysis.features.normalized.split())
    if len(sql) > _DETAIL_SQL_LENGTH:
        sql = sql[:_DETAIL_SQL_LENGTH - 3] + '...'
    context = ', '.join(f'{key}={value}' for key, value in (('user', record.user), ('at', record.at)) if value)
    return f'{sql} ({context})' if context else sql


# ---------------------------------------------------------------------------------------------- ids and names
def definition_id(kind: Union[Kind, str], expression: str, conditions: Iterable[str] = (),
                  tables: Iterable[str] = (), dialect: Optional[str] = None) -> str:
    """Id of an induced definition: :func:`model.make_id`, with ``tables`` added to the key when
    ``expression`` reads no column (COUNT(*) over orders and over customers are different measures)."""
    key = expression
    tables = sorted(tables)
    if tables and not _reads_columns(expression, dialect):
        key = f"{expression} FROM {', '.join(tables)}"
    return make_id(Kind(kind), key, conditions)


def definition_name(kind: Union[Kind, str], expression: str, conditions: Iterable[str] = (),
                    tables: Iterable[str] = (), dialect: Optional[str] = None) -> str:
    """Readable snake_case name built from the fragment.

    Examples: 'sum_orders_amount', 'count_orders' (COUNT(*) over ``tables``), 'orders_status_eq_paid',
    'join_customers_orders', 'sum_orders_amount_where_orders_status_eq_paid'. Names longer than
    MAX_NAME_LENGTH are cut and end with an 8-character hash, so they stay distinct.
    """
    kind = Kind(kind)
    conditions = list(conditions)
    if kind is Kind.JOIN:
        name = _join_name(expression, dialect)
    else:
        name = _fragment_slug(expression, tuple(tables), dialect)
        if kind is Kind.METRIC and conditions:
            name += '_where_' + '_and_'.join(_fragment_slug(c, (), dialect) for c in conditions)
    return _shorten(name or kind.value, '|'.join([kind.value, expression] + conditions))


def _join_name(expression: str, dialect: Optional[str]) -> str:
    node = _parse_fragment(expression, dialect)
    if isinstance(node, exp.EQ) and isinstance(node.this, exp.Column) and isinstance(node.expression, exp.Column):
        sides = [_slug('_'.join(p.name for p in c.parts[:-1])) for c in (node.this, node.expression)]
        if all(sides):
            return 'join_' + '_'.join(sides)
    return 'join_' + _fragment_slug(expression, (), dialect)


def _fragment_slug(sql: str, tables: Tuple[str, ...], dialect: Optional[str]) -> str:
    node = _parse_fragment(sql, dialect)
    if node is None:
        return _slug(sql)
    try:
        return _slug('_'.join(_tokens(node, tables, dialect)))
    except RecursionError:
        return _slug(sql)


@lru_cache(maxsize=16384)
def _parse_fragment(sql: str, dialect: Optional[str]) -> Optional[exp.Expression]:
    try:
        return sqlglot.parse_one(sql, read=dialect)
    except Exception:  # noqa: BLE001 - names fall back to the raw text
        return None


def _reads_columns(expression: str, dialect: Optional[str]) -> bool:
    node = _parse_fragment(expression, dialect)
    return node is None or node.find(exp.Column) is not None


_OPERATOR_WORDS = {
    exp.EQ: 'eq', exp.NEQ: 'ne', exp.GT: 'gt', exp.GTE: 'ge', exp.LT: 'lt', exp.LTE: 'le', exp.Is: 'is',
    exp.Like: 'like', exp.ILike: 'ilike', exp.Add: 'plus', exp.Sub: 'minus', exp.Mul: 'times', exp.Div: 'div',
    exp.Mod: 'mod', exp.DPipe: 'concat', exp.NullSafeEQ: 'eq',
}


def _tokens(node: exp.Expression, tables: Tuple[str, ...], dialect: Optional[str]) -> List[str]:
    """Words for a readable name, in reading order."""
    def words(child: Optional[exp.Expression]) -> List[str]:
        return [] if child is None else _tokens(child, tables, dialect)

    if isinstance(node, exp.Column):
        names = [p.name for p in node.parts]
        return names[:-1] + ['all'] if isinstance(node.this, exp.Star) else names
    if isinstance(node, (exp.Literal, exp.Identifier, exp.Var)):
        return [str(node.name if not isinstance(node, exp.Literal) else node.this)]
    if isinstance(node, exp.Null):
        return ['null']
    if isinstance(node, exp.Boolean):
        return ['true' if node.this else 'false']
    if isinstance(node, (exp.Placeholder, exp.Parameter)):
        return ['param']
    if isinstance(node, exp.Star):
        return ['all']
    if isinstance(node, (exp.Paren, exp.Cast)):
        return words(node.this)
    if isinstance(node, exp.Neg):
        return ['minus'] + words(node.this)
    if isinstance(node, (exp.And, exp.Or)):
        joiner = 'and' if isinstance(node, exp.And) else 'or'
        out: List[str] = []
        for i, operand in enumerate(node.flatten()):
            out += ([joiner] if i else []) + words(operand)
        return out
    if isinstance(node, exp.Not):
        inner = node.this.unnest() if isinstance(node.this, exp.Paren) else node.this
        if isinstance(inner, exp.Is):
            return words(inner.this) + ['is', 'not'] + words(inner.expression)
        if isinstance(inner, (exp.In, exp.Between)):
            return _range_words(inner, ['not'], words)
        return ['not'] + words(inner)
    if isinstance(node, (exp.Like, exp.ILike)) and node.args.get('negate'):
        return words(node.this) + ['not', _OPERATOR_WORDS[type(node)]] + words(node.expression)
    if isinstance(node, exp.Binary):
        return words(node.left) + [_OPERATOR_WORDS.get(type(node), node.key)] + words(node.right)
    if isinstance(node, (exp.In, exp.Between)):
        return _range_words(node, [], words)
    if isinstance(node, exp.Exists):
        return ['exists']
    if isinstance(node, (exp.Subquery, exp.Select)):
        return ['subquery']
    if isinstance(node, exp.Distinct):
        return ['distinct'] + [w for e in node.expressions for w in words(e)]
    if isinstance(node, exp.Filter):
        where = node.expression
        return words(node.this) + ['filter'] + words(where.this if isinstance(where, exp.Where) else where)
    if isinstance(node, exp.Count) and isinstance(node.this, exp.Star):
        return ['count'] + ([_slug(t) for t in tables] or ['rows'])
    if isinstance(node, exp.Case):
        out = ['case']
        for branch in node.args.get('ifs') or []:
            out += ['when'] + words(branch.this) + ['then'] + words(branch.args.get('true'))
        if node.args.get('default') is not None:
            out += ['else'] + words(node.args['default'])
        return out
    if isinstance(node, exp.Func):
        return _text_words(node.sql(dialect=dialect), dialect)   # as written: STRFTIME('%Y', x), not sqlglot's tree
    return [w for child in node.iter_expressions() for w in words(child)]


def _range_words(node: exp.Expression, negation: List[str], words: Any) -> List[str]:
    """Words for ``x [NOT] IN (...)`` and ``x [NOT] BETWEEN a AND b``."""
    if isinstance(node, exp.In):
        values = ['subquery'] if node.args.get('query') else [w for v in node.expressions for w in words(v)]
        return words(node.this) + negation + ['in'] + values
    return (words(node.this) + negation + ['between'] + words(node.args.get('low')) + ['and']
            + words(node.args.get('high')))


_TOKEN_WORDS = {'EQ': 'eq', 'NEQ': 'ne', 'GT': 'gt', 'GTE': 'ge', 'LT': 'lt', 'LTE': 'le', 'PLUS': 'plus',
                'DASH': 'minus', 'SLASH': 'div', 'MOD': 'mod', 'STAR': 'all', 'DPIPE': 'concat'}
_PUNCTUATION = frozenset({'L_PAREN', 'R_PAREN', 'COMMA', 'DOT', 'L_BRACKET', 'R_BRACKET', 'SEMICOLON', 'COLON'})
_STAR_MEANS_ALL_AFTER = frozenset({'L_PAREN', 'COMMA', 'DOT'})   # COUNT(*), f(a, *), t.*; otherwise a * b


def _text_words(sql: str, dialect: Optional[str]) -> List[str]:
    """Words of a SQL snippet in written order: names and literals as written, operators as words."""
    try:
        tokens = Dialect.get_or_raise(dialect).tokenize(sql)
    except Exception:  # noqa: BLE001 - fall back to the raw text
        return [sql]
    out: List[str] = []
    previous = None
    for t in tokens:
        name = t.token_type.name
        if name == 'STAR' and previous is not None and previous not in _STAR_MEANS_ALL_AFTER:
            out.append('times')
        elif name not in _PUNCTUATION:
            out.append(_TOKEN_WORDS.get(name, t.text))
        previous = name
    return out


def _slug(text: str) -> str:
    return re.sub(r'_+', '_', re.sub(r'\W+', '_', str(text).lower())).strip('_')


def _shorten(name: str, key: str) -> str:
    if len(name) <= MAX_NAME_LENGTH:
        return name
    digest = hashlib.sha1(key.encode('utf-8')).hexdigest()[:8]
    return name[:MAX_NAME_LENGTH - len(digest) - 1].rstrip('_') + '_' + digest
