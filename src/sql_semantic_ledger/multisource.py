"""Several evidence sources and products (stage 2): metric-store definitions and cross-source conflicts.

CONTRACT (implemented by the build step):

    load_metric_yaml(path, product=None) -> list[Definition]
        Minimal schema (PyYAML needed, extra 'yaml'):
            metrics:
              - name: active_customers
                expression: COUNT(DISTINCT orders.customer_id)
                filters: ["orders.status = 'paid'"]
                tables: [orders]
                description: optional
        Each metric becomes Definition(kind=METRIC, source='yaml:<product or file stem>') with one
        Evidence(kind=CODE, ref='<path>#<name>', detail='metric store').
    detect_conflicts(definitions) -> list[Conflict]
        Groups definitions by normalized name (lower case, spaces/hyphens -> '_'); a group conflicts when it
        holds more than one distinct (expression, sorted conditions) pair. ``kinds`` lists what differs:
        'expression', 'conditions', 'tables'.
    load_dbt_semantic_models(...)   planned (full dbt MetricFlow / metric-hub schemas)

Details of the YAML schema
--------------------------
* ``metrics`` may be a list of metrics or a mapping ``name -> metric``. Each metric needs a non-empty
  ``name`` and ``expression``. ``filters`` (alias ``conditions``) and ``tables`` take a string or a list of
  strings. ``description`` goes to ``meta['description']``, and ``valid_from``/``valid_until`` (a string
  or a YAML date) go to the definition's validity period. Other keys are ignored.
* One file may describe several products: ``products: {web: {metrics: [...]}, app: {metrics: [...]}}``,
  or a ``product`` key on a metric. The product of a metric is the first of: its own ``product`` key, its
  ``products`` entry, the ``product`` argument, a top-level ``product`` key, the file stem.
* Fragments are written the way :mod:`sql_semantic_ledger.parse` writes induced fragments
  (:func:`parse.normalize_expression`, sqlglot's default dialect): names in lower case unless quoted, the
  column side of a comparison on the left, AND/OR operands and literal IN lists sorted, ``x NOT LIKE y``.
  So formatting does not create differences, and a metric-store definition gets the same id as the same
  definition induced from SQL (:func:`induce.definition_id`), so that their evidence merges in a ledger.
  Filters are split on top-level AND, de-duplicated and sorted; an OR group is one condition. A fragment
  sqlglot cannot parse is kept with its whitespace collapsed.
* ``tables`` are written the same way (``Orders`` -> ``orders``); missing ``tables`` are inferred from
  qualified columns. When the metric has exactly one table, unqualified columns outside subqueries are
  qualified with it (``AVG(amount)`` -> ``AVG(orders.amount)``). ``columns`` lists the qualified
  ``table.column`` references as SQL text.
* The evidence ref is ``'<path>#<name>'`` (path in POSIX form). In a file that defines more than one
  product it is ``'<path>#<product>/<name>'``, so that identical definitions in two products stay two
  independent sites.

Conflicts
---------
Expressions and AND-split conditions are compared after the same normalization (unqualified columns are
qualified when a definition has exactly one table), so only differences in meaning count, not formatting.
In ``Conflict.expressions`` an OR condition is shown in parentheses. Every definition passed in is compared,
whatever its validity period; pass only the ones that should agree (e.g. currently valid ones).
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Union

import sqlglot
from sqlglot import exp

from . import _names
from ._unimplemented import planned
from .induce import definition_id
from .model import Definition, Evidence, EvidenceKind, Kind
from .parse import normalize_expression

_DIFF_KINDS = ('expression', 'conditions', 'tables')
_LOW_PRECEDENCE = tuple(c for c in (getattr(exp, 'Or', None), getattr(exp, 'Xor', None)) if c is not None)
_PROBE = 'ledger_probe_column'   # column name used to normalize a table name as a column qualifier


@dataclass
class Conflict:
    """Definitions that share a normalized name but differ in expression or conditions.

    ``name`` is the normalized name. ``definition_ids``, ``sources`` and ``expressions`` are parallel lists,
    one entry per definition, ordered by (source, rendered definition, id). Each entry of ``expressions``
    is the definition's normalized expression followed by ``WHERE`` and its conditions, so every variant can
    be read in full. ``kinds`` lists what differs, in the order 'expression', 'conditions', 'tables'.
    """

    name: str
    definition_ids: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)
    expressions: List[str] = field(default_factory=list)
    kinds: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------------------------- public API
def load_metric_yaml(path: Union[str, Path], product: Optional[str] = None) -> List[Definition]:
    """Metric definitions from a metric-store YAML file (schema in the module docstring).

    Args:
        path: the YAML file.
        product: product name for metrics that do not name their own (see the module docstring for the
            order of precedence); by default the file's ``product`` key, then the file stem.

    Returns:
        One METRIC definition per metric, in file order, each with source ``'yaml:<product>'`` and one
        ``Evidence(kind=CODE, ref=..., detail='metric store')``.

    Raises:
        ImportError: PyYAML is not installed.
        FileNotFoundError: ``path`` does not exist.
        ValueError: the file is not valid YAML or does not follow the schema.
    """
    if product is not None and (not isinstance(product, str) or not product.strip()):
        raise ValueError('product must be a non-empty string or None')
    yaml = _yaml_module()
    file_path = Path(path)
    where = file_path.as_posix()
    text = file_path.read_text(encoding='utf-8')
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f'{where}: not valid YAML: {exc}') from exc
    entries = _yaml_entries(data, where, product.strip() if product else None, file_path.stem)
    multi = len({prod for prod, _, _ in entries}) > 1
    return [_metric_definition(entry, prod, where, loc, multi) for prod, entry, loc in entries]


def detect_conflicts(definitions: Iterable[Definition]) -> List[Conflict]:
    """Groups of same-named definitions that disagree (see the contract above), sorted by name."""
    groups: Dict[str, List[Definition]] = {}
    seen: Set[Tuple[str, str]] = set()
    for d in definitions:
        if (d.id, d.source) in seen:     # the same definition passed twice
            continue
        seen.add((d.id, d.source))
        groups.setdefault(_normalize_name(d.name), []).append(d)
    conflicts: List[Conflict] = []
    for name in sorted(groups):
        conflict = _group_conflict(name, groups[name])
        if conflict is not None:
            conflicts.append(conflict)
    return conflicts


def load_dbt_semantic_models(*args, **kwargs):
    """Full dbt MetricFlow / metric-hub schemas. Planned (stage 2)."""
    planned('make.multisource')


# ---------------------------------------------------------------------------------------------- YAML
def _yaml_module() -> Any:
    """Import PyYAML lazily, with an installation hint if it is missing."""
    try:
        import yaml  # noqa: PLC0415 - optional dependency, imported only when needed
    except ImportError as exc:
        raise ImportError('load_metric_yaml needs PyYAML: pip install "sql-semantic-ledger[yaml]" '
                          '(or pip install PyYAML)') from exc
    return yaml


def _yaml_entries(data: Any, where: str, product: Optional[str], stem: str) -> List[Tuple[str, Any, str]]:
    """(product, metric entry, location label) for every metric in the parsed file."""
    if data is None:
        return []
    if not isinstance(data, dict):
        raise ValueError(f'{where}: the top level must be a mapping with a "metrics" list')
    if 'metrics' not in data and 'products' not in data:
        raise ValueError(f'{where}: no "metrics" (or "products") key at the top level')
    file_product = _optional_name(data.get('product'), where, 'product')
    default = product or file_product or stem
    entries = [(_entry_product(e, default, where, loc), e, loc)
               for loc, e in _metric_list(data.get('metrics'), where, 'metrics')]
    products = data.get('products')
    if products is not None:
        if not isinstance(products, dict):
            raise ValueError(f'{where}: "products" must be a mapping of product name to metrics')
        for key, body in products.items():
            name = str(key).strip()
            if not name:
                raise ValueError(f'{where}: empty product name under "products"')
            metrics = body.get('metrics') if isinstance(body, dict) else body
            for loc, e in _metric_list(metrics, where, f'products.{name}.metrics'):
                entries.append((_entry_product(e, name, where, loc), e, loc))
    return entries


def _metric_list(value: Any, where: str, label: str) -> List[Tuple[str, Any]]:
    """(location label, entry) pairs of a metrics list or a ``name -> metric`` mapping."""
    if value is None:
        return []
    if isinstance(value, list):
        return [(f'{label}[{i}]', e) for i, e in enumerate(value)]
    if isinstance(value, dict):
        out = []
        for key, body in value.items():
            if body is None:
                body = {}
            if not isinstance(body, dict):
                raise ValueError(f'{where} {label}.{key}: a metric must be a mapping')
            entry = dict(body)
            entry.setdefault('name', str(key))
            out.append((f'{label}.{key}', entry))
        return out
    raise ValueError(f'{where}: "{label}" must be a list of metrics or a mapping of name to metric')


def _entry_product(entry: Any, default: str, where: str, loc: str) -> str:
    if isinstance(entry, dict):
        return _optional_name(entry.get('product'), where, f'{loc}.product') or default
    return default


def _optional_name(value: Any, where: str, label: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{where} {label}: must be a non-empty string')
    return value.strip()


def _metric_definition(entry: Any, product: str, where: str, loc: str, multi: bool) -> Definition:
    """One metric entry as a METRIC definition with its metric-store evidence."""
    if not isinstance(entry, dict):
        raise ValueError(f'{where} {loc}: a metric must be a mapping, not {type(entry).__name__}')
    name = _required_text(entry, 'name', where, loc)
    raw_expression = _required_text(entry, 'expression', where, loc)
    filters = _text_list(entry.get('filters'), 'filters', where, loc)
    filters += _text_list(entry.get('conditions'), 'conditions', where, loc)
    declared = {_canon_table(t) for t in _text_list(entry.get('tables'), 'tables', where, loc)}
    tables = sorted(declared or _qualifier_tables([raw_expression] + filters))
    only = tables[0] if len(tables) == 1 else None
    expression = _canon_expression(raw_expression, only)
    conditions = sorted({leaf for f in filters for leaf in _condition_leaves(f, only)})
    meta: Dict[str, Any] = {'product': product}
    if entry.get('description') is not None:
        meta['description'] = str(entry['description']).strip()
    ref = f'{where}#{product}/{name}' if multi else f'{where}#{name}'
    return Definition(name=name, kind=Kind.METRIC, expression=expression, conditions=conditions,
                      tables=tables, columns=_qualified_columns([expression] + conditions), source=f'yaml:{product}',
                      evidence=[Evidence(EvidenceKind.CODE, detail='metric store', ref=ref)],
                      valid_from=_iso(entry.get('valid_from'), where, f'{loc}.valid_from'),
                      valid_until=_iso(entry.get('valid_until'), where, f'{loc}.valid_until'), meta=meta,
                      id=definition_id(Kind.METRIC, expression, conditions, tables))


def _required_text(entry: Dict[str, Any], key: str, where: str, loc: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{where} {loc}: a metric needs a non-empty string "{key}"')
    return value.strip()


def _text_list(value: Any, key: str, where: str, loc: str) -> List[str]:
    """A string or a list of strings as a list of non-empty stripped strings."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f'{where} {loc}: "{key}" must be a string or a list of strings')
    return [v.strip() for v in value if v.strip()]


def _iso(value: Any, where: str, label: str) -> Optional[str]:
    """A validity bound as an ISO-8601 string (YAML may give a date or datetime)."""
    if value is None:
        return None
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError(f'{where} {label}: must be a date or an ISO-8601 string')


# ---------------------------------------------------------------------------------------------- SQL fragments
def _parse_fragment(sql: str) -> Optional[exp.Expression]:
    text = sql.strip()
    if not text:
        return None
    try:
        return sqlglot.parse_one(text)
    except Exception:  # noqa: BLE001 - any parser failure means "keep the text as written"
        return None


def _squash(sql: str) -> str:
    return ' '.join(sql.split())


def _normalized(sql: str, table: Optional[str] = None) -> Optional[str]:
    """``sql`` as :func:`parse.normalize_expression` writes it, with unqualified columns outside subqueries
    qualified by ``table`` (SQL text) when it is given; None when the fragment does not parse."""
    try:
        text = normalize_expression(sql)
    except ValueError:
        return None
    if table is None:
        return text
    node = sqlglot.parse_one(text)
    bare = [c for c in node.find_all(exp.Column) if not c.table and isinstance(c.this, exp.Identifier)
            and c.find_ancestor(exp.Select) is None]
    parts = [exp.Identifier(this=name, quoted=quoted) for name, quoted in _names.table_parts(table)]
    if not bare or not 1 <= len(parts) <= 3:
        return text
    for column in bare:
        for key, ident in zip(('catalog', 'db', 'table')[-len(parts):], parts):
            column.set(key, ident.copy())
    return normalize_expression(node.sql())


def _canon_expression(sql: str, table: Optional[str] = None) -> str:
    """Normalized expression (see the module docstring); whitespace-collapsed text if it does not parse."""
    text = _normalized(sql, table)
    return text if text is not None else _squash(sql)


def _condition_leaves(sql: str, table: Optional[str] = None) -> List[str]:
    """A condition split on top-level AND into normalized predicates (an OR group is one predicate)."""
    node = _parse_fragment(sql)
    if node is None:
        text = _squash(sql)
        return [text] if text else []
    leaves: List[str] = []
    stack = [node]
    while stack:
        cur = stack.pop()
        while isinstance(cur, exp.Paren):
            cur = cur.this
        if isinstance(cur, exp.And):
            stack.extend([cur.right, cur.left])
        else:
            text = cur.sql()
            leaves.append(_normalized(text, table) or text)
    return leaves


def _canon_table(name: str) -> str:
    """A table name written as :mod:`parse` writes them (``Orders`` -> ``orders``, ``"Order Items"`` kept);
    text that is not a plain table name becomes one quoted name (``order items`` -> ``"order items"``)."""
    text = _names.table_sql(name)
    column = _parse_fragment(_normalized(f'{text}.{_PROBE}') or '')
    if not (isinstance(column, exp.Column) and column.name == _PROBE and column.table):
        return text
    return _column_table(column)


def _column_table(column: exp.Column) -> str:
    """The table that qualifies ``column``, as SQL text."""
    return exp.Table(this=column.args['table'].copy(), db=_copy(column.args.get('db')),
                     catalog=_copy(column.args.get('catalog'))).sql()


def _qualifier_tables(fragments: Iterable[str]) -> Set[str]:
    """Tables that qualify columns in the fragments (after normalization)."""
    tables: Set[str] = set()
    for fragment in fragments:
        node = _parse_fragment(_normalized(fragment) or '')
        if node is not None:
            tables |= {_column_table(c) for c in node.find_all(exp.Column) if c.table and c.name and c.name != '*'}
    return tables


def _qualified_columns(fragments: Iterable[str]) -> List[str]:
    """Sorted ``table.column`` references (SQL text) of the normalized fragments, subqueries included."""
    columns: Set[str] = set()
    for fragment in fragments:
        node = _parse_fragment(fragment)
        if node is None:
            continue
        for col in node.find_all(exp.Column):
            if col.table and isinstance(col.this, exp.Identifier) and col.name:
                columns.add(exp.Column(this=col.this.copy(), table=col.args['table'].copy(),
                                       db=_copy(col.args.get('db')), catalog=_copy(col.args.get('catalog'))).sql())
    return sorted(columns)


def _copy(node: Optional[exp.Expression]) -> Optional[exp.Expression]:
    return node.copy() if node is not None else None


# ---------------------------------------------------------------------------------------------- conflicts
def _normalize_name(name: str) -> str:
    """Grouping key: lower case, runs of spaces and hyphens become one '_'."""
    return re.sub(r'[\s\-]+', '_', name.strip().lower())


def _tables(d: Definition) -> Tuple[str, ...]:
    """The definition's tables, normalized and sorted."""
    return tuple(sorted({_canon_table(t) for t in d.tables if t and t.strip()}))


def _variant(d: Definition) -> Tuple[str, Tuple[str, ...]]:
    """(normalized expression, sorted normalized conditions) of a definition; unqualified columns are
    qualified when the definition has exactly one table."""
    tables = _tables(d)
    only = tables[0] if len(tables) == 1 else None
    leaves = {leaf for c in d.conditions for leaf in _condition_leaves(c, only)}
    return _canon_expression(d.expression, only), tuple(sorted(leaves))


def _render(variant: Tuple[str, Tuple[str, ...]]) -> str:
    expression, conditions = variant
    shown = [_parenthesized(c) for c in conditions]
    return f'{expression} WHERE {" AND ".join(shown)}' if shown else expression


def _parenthesized(condition: str) -> str:
    """An OR condition in parentheses, so that ``a = 1 AND (b = 2 OR c = 3)`` reads right."""
    return f'({condition})' if isinstance(_parse_fragment(condition), _LOW_PRECEDENCE) else condition


def _group_conflict(name: str, defs: List[Definition]) -> Optional[Conflict]:
    """The conflict of one same-name group, or None if all its definitions agree."""
    rows = [(d, _variant(d)) for d in defs]
    distinct = {v for _, v in rows}
    if len(distinct) < 2:
        return None
    differs = {
        'expression': len({v[0] for v in distinct}) > 1,
        'conditions': len({v[1] for v in distinct}) > 1,
        'tables': len({_tables(d) for d in defs}) > 1,
    }
    rows.sort(key=lambda row: (row[0].source, _render(row[1]), row[0].id))
    return Conflict(name=name, definition_ids=[d.id for d, _ in rows], sources=[d.source for d, _ in rows],
                    expressions=[_render(v) for _, v in rows], kinds=[k for k in _DIFF_KINDS if differs[k]])
