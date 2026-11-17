"""Tests for multisource: metric-store YAML definitions and cross-source / cross-product conflicts."""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from sql_semantic_ledger import Definition, EvidenceKind, Grade, Kind, Ledger
from sql_semantic_ledger._unimplemented import NotImplementedYet
from sql_semantic_ledger.multisource import Conflict, detect_conflicts, load_dbt_semantic_models, load_metric_yaml

pytest.importorskip('yaml')

TWO_PRODUCTS = """\
products:
  web:
    metrics:
      - name: active_customers
        expression: count(distinct orders.customer_id)
        filters: ["orders.status = 'paid'"]
        tables: [orders]
        description: Customers with a paid order
  app:
    metrics:
      - name: Active Customers
        expression: COUNT(DISTINCT orders.customer_id)
        filters:
          - orders.status = 'paid'
          - orders.channel = 'app'
        tables: [orders]
"""


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(text), encoding='utf-8')
    return path


def metric(name: str, expression: str, conditions=(), tables=('orders',), source: str = 'history') -> Definition:
    return Definition(name=name, kind=Kind.METRIC, expression=expression, conditions=list(conditions),
                      tables=list(tables), source=source)


# ------------------------------------------------------------------------------------------- load_metric_yaml
def test_one_file_two_products_conflict_on_conditions(tmp_path: Path):
    path = write(tmp_path, 'metrics.yaml', TWO_PRODUCTS)
    web, app = load_metric_yaml(path)
    where = path.as_posix()
    assert (web.source, app.source) == ('yaml:web', 'yaml:app')
    assert [e.ref for e in web.evidence + app.evidence] == [f'{where}#web/active_customers',
                                                         f'{where}#app/Active Customers']
    assert web.meta == {'product': 'web', 'description': 'Customers with a paid order'}

    conflicts = detect_conflicts([web, app])
    assert len(conflicts) == 1
    c = conflicts[0]
    assert isinstance(c, Conflict)
    assert c.name == 'active_customers' and c.kinds == ['conditions']
    assert c.sources == ['yaml:app', 'yaml:web'] and c.definition_ids == [app.id, web.id]
    assert c.expressions == [
        "COUNT(DISTINCT orders.customer_id) WHERE orders.channel = 'app' AND orders.status = 'paid'",
        "COUNT(DISTINCT orders.customer_id) WHERE orders.status = 'paid'",
    ]


def test_metric_fields_follow_the_contract(tmp_path: Path):
    path = write(tmp_path, 'shop_metrics.yml', """\
        product: shop
        metrics:
          - name: paid_revenue
            expression: sum(orders.amount)
            filters: "orders.status = 'paid' AND orders.is_test = 0"
            description: Revenue from paid orders
          - name: avg_basket
            expression: AVG(amount)
            tables: orders
            valid_from: 2024-01-01
        """)
    paid, basket = load_metric_yaml(path)
    assert paid.kind == Kind.METRIC and paid.source == 'yaml:shop'
    assert paid.expression == 'SUM(orders.amount)'
    assert paid.conditions == ['orders.is_test = 0', "orders.status = 'paid'"]
    assert paid.tables == ['orders']                                  # inferred from qualified columns
    assert paid.columns == ['orders.amount', 'orders.is_test', 'orders.status']
    assert paid.meta == {'product': 'shop', 'description': 'Revenue from paid orders'}
    ev, = paid.evidence
    assert (ev.kind, ev.ref, ev.detail, ev.passed, ev.version) == (
        EvidenceKind.CODE, f'{path.as_posix()}#paid_revenue', 'metric store', True, 1)
    assert paid.support == 1 and paid.grade == Grade.E0 and paid.sources == ['code']
    assert basket.expression == 'AVG(orders.amount)' and basket.conditions == []   # qualified by the only table
    assert basket.tables == ['orders'] and basket.columns == ['orders.amount']
    assert basket.valid_from == '2024-01-01' and basket.valid_until is None
    # the argument overrides the file-level product
    assert {d.source for d in load_metric_yaml(path, product='store')} == {'yaml:store'}


def test_product_defaults_to_the_file_stem_and_expression_conflict(tmp_path: Path):
    web = write(tmp_path, 'web.yaml', """\
        metrics:
          - name: active-customers
            expression: COUNT(DISTINCT orders.customer_id)
            filters: ["orders.status = 'paid'"]
            tables: [orders]
        """)
    app = write(tmp_path, 'app.yaml', """\
        metrics:
          - name: active_customers
            expression: COUNT(orders.customer_id)
            filters: ["orders.status = 'paid'"]
            tables: [orders]
        """)
    defs = load_metric_yaml(web) + load_metric_yaml(app)
    assert [d.source for d in defs] == ['yaml:web', 'yaml:app']
    assert defs[0].evidence[0].ref == f'{web.as_posix()}#active-customers'   # single product: '<path>#<name>'
    conflict, = detect_conflicts(defs)
    assert conflict.name == 'active_customers' and conflict.kinds == ['expression']
    assert conflict.sources == ['yaml:app', 'yaml:web']


def test_agreeing_products_do_not_conflict_and_count_as_two_sites(tmp_path: Path):
    a = write(tmp_path, 'web.yaml', """\
        metrics:
          - name: active_customers
            expression: count(distinct orders.customer_id)
            filters: ["orders.status='paid'", "orders.is_test = 0"]
        """)
    b = write(tmp_path, 'app.yaml', """\
        metrics:
          - name: Active Customers
            expression: COUNT( DISTINCT   orders.customer_id )
            filters: "orders.is_test = 0 AND orders.status = 'paid'"
        """)
    defs = load_metric_yaml(a) + load_metric_yaml(b)
    assert detect_conflicts(defs) == []
    assert defs[0].id == defs[1].id                     # same normalized definition
    led = Ledger()
    for d in defs:
        led.add(d)
    merged, = list(led)
    assert merged.support == 2 and merged.grade == Grade.E1 and merged.sources == ['code']


def test_or_group_is_one_condition_written_as_induce_writes_it(tmp_path: Path):
    path = write(tmp_path, 'm.yaml', """\
        metrics:
          - name: shipped_or_paid
            expression: COUNT(*)
            filters: ["orders.status = 'shipped' OR orders.status = 'paid'", "orders.amount > 0"]
            tables: [orders]
        """)
    d, = load_metric_yaml(path)
    assert d.conditions == ['orders.amount > 0', "orders.status = 'paid' OR orders.status = 'shipped'"]
    assert d.columns == ['orders.amount', 'orders.status']
    other = metric('shipped_or_paid', 'COUNT(*)', ["orders.status = 'paid'"])
    conflict, = detect_conflicts([d, other])
    assert "COUNT(*) WHERE orders.amount > 0 AND (orders.status = 'paid' OR orders.status = 'shipped')" in \
        conflict.expressions                                     # an OR condition is shown in parentheses


@pytest.mark.parametrize('first, second', [
    ("orders.status = 'paid'", "'paid' = orders.status"),                         # operand order
    ("orders.status = 'paid'", "Orders.Status = 'paid'"),                         # case of unquoted names
    ("orders.status IN ('paid', 'refunded')", "orders.status IN ('refunded', 'paid')"),
    ("orders.note NOT LIKE '%test%'", "NOT orders.note LIKE '%test%'"),
    ("orders.status = 'paid'", "status = 'paid'"),                                # qualified by the only table
])
def test_formatting_alone_never_conflicts(tmp_path: Path, first: str, second: str):
    a = write(tmp_path, 'a.yaml', f'metrics:\n  - name: m\n    expression: SUM(orders.amount)\n'
                                  f'    filters: ["{first}"]\n    tables: [orders]\n')
    b = write(tmp_path, 'b.yaml', f'metrics:\n  - name: m\n    expression: sum(Orders.Amount)\n'
                                  f'    filters: ["{second}"]\n    tables: [Orders]\n')
    defs = load_metric_yaml(a) + load_metric_yaml(b)
    assert detect_conflicts(defs) == []
    assert defs[0].id == defs[1].id and defs[0].conditions == defs[1].conditions
    assert defs[0].tables == defs[1].tables == ['orders']


def test_metric_store_ids_match_induced_definitions(tmp_path: Path):
    from sql_semantic_ledger.induce import induce

    path = write(tmp_path, 'store.yaml', """\
        metrics:
          - name: paid_revenue
            expression: SUM(amount)
            filters: ["'paid' = status", "is_test = 0"]
            tables: [orders]
          - name: paid_or_shipped_orders
            expression: COUNT(1)
            filters: ["status = 'shipped' OR status = 'paid'"]
            tables: [Orders]
        """)
    led = induce(["SELECT SUM(o.amount) FROM orders o WHERE o.status = 'paid' AND o.is_test = 0",
                  "SELECT c.region, SUM(o.amount) FROM orders o JOIN customers c ON c.id = o.customer_id "
                  "WHERE o.is_test = 0 AND o.status = 'paid' GROUP BY c.region",
                  "SELECT COUNT(*) FROM orders WHERE status = 'paid' OR status = 'shipped'",
                  "SELECT COUNT(*) FROM orders WHERE (status = 'shipped' OR status = 'paid') AND amount > 0"])
    paid, shipped = load_metric_yaml(path)
    assert paid.id in led and shipped.id in led                # the same definitions, so the evidence merges
    for d in (paid, shipped):
        led.add(d)
    assert led.get(paid.id).sources == ['code', 'history'] and led.get(paid.id).support == 3
    assert led.get(shipped.id).expression == 'COUNT(*)' and led.get(shipped.id).support == 3


def test_metrics_mapping_form_and_per_metric_product(tmp_path: Path):
    path = write(tmp_path, 'store.yaml', """\
        metrics:
          active_customers:
            expression: COUNT(DISTINCT orders.customer_id)
            product: web
          churned_customers:
            expression: COUNT(DISTINCT churn.customer_id)
        """)
    active, churned = load_metric_yaml(path)
    assert (active.name, active.source, churned.name, churned.source) == (
        'active_customers', 'yaml:web', 'churned_customers', 'yaml:store')
    assert churned.tables == ['churn'] and churned.columns == ['churn.customer_id']
    assert active.evidence[0].ref.endswith('#web/active_customers')       # two products in one file


def test_unparseable_fragment_is_kept_as_written(tmp_path: Path):
    path = write(tmp_path, 'odd.yaml', 'metrics:\n  - name: odd\n    expression: "COUNT(DISTINCT   x"\n')
    d, = load_metric_yaml(path)
    assert d.expression == 'COUNT(DISTINCT x' and d.tables == [] and d.columns == []


def test_empty_file_gives_no_definitions(tmp_path: Path):
    assert load_metric_yaml(write(tmp_path, 'empty.yaml', '# nothing yet\n')) == []
    assert load_metric_yaml(write(tmp_path, 'none.yaml', 'metrics:\n')) == []


@pytest.mark.parametrize('text, message', [
    ('metrics: [unclosed\n', 'not valid YAML'),
    ('- a\n- b\n', 'top level'),
    ('foo: 1\n', 'metrics'),
    ('metrics:\n  - name: x\n', 'expression'),
    ('metrics:\n  - expression: COUNT(*)\n', 'name'),
    ('metrics: [just_a_string]\n', 'mapping'),
    ('metrics: 3\n', 'list of metrics'),
    ('metrics:\n  - name: x\n    expression: COUNT(*)\n    filters: {a: 1}\n', 'filters'),
    ('metrics:\n  - name: x\n    expression: COUNT(*)\n    valid_from: [1]\n', 'valid_from'),
    ('products: [web]\n', 'products'),
    ('product: 3\nmetrics: []\n', 'product'),
])
def test_schema_errors_are_reported(tmp_path: Path, text: str, message: str):
    with pytest.raises(ValueError, match=message):
        load_metric_yaml(write(tmp_path, 'bad.yaml', text))


def test_missing_file_bad_argument_and_missing_pyyaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(FileNotFoundError):
        load_metric_yaml(tmp_path / 'missing.yaml')
    path = write(tmp_path, 'ok.yaml', 'metrics: []\n')
    with pytest.raises(ValueError, match='product'):
        load_metric_yaml(path, product='  ')
    monkeypatch.setitem(sys.modules, 'yaml', None)            # simulate an environment without PyYAML
    with pytest.raises(ImportError, match='PyYAML'):
        load_metric_yaml(path)


# ------------------------------------------------------------------------------------------- detect_conflicts
def test_conflict_across_sources_lists_tables_when_they_differ():
    hist = metric('active customers', 'COUNT(DISTINCT orders.customer_id)', ["orders.status = 'paid'"])
    manual = metric('Active-Customers', 'COUNT(DISTINCT orders.customer_id)',
                    ["orders.status = 'paid'", 'customers.is_test = 0'], tables=('orders', 'customers'),
                    source='manual')
    conflict, = detect_conflicts([manual, hist])
    assert conflict.kinds == ['conditions', 'tables'] and conflict.sources == ['history', 'manual']
    assert conflict.definition_ids == [hist.id, manual.id]


def test_tables_alone_or_formatting_alone_do_not_conflict():
    a = metric('revenue', 'SUM(orders.amount)', ["orders.status = 'paid'"], tables=('orders',))
    b = metric('revenue', 'SUM(orders.amount)', ["orders.status = 'paid'"], tables=(), source='manual')
    c = metric('Revenue', 'sum(orders.amount)', ["orders.status='paid'"], source='yaml:web')
    assert detect_conflicts([a, b, c]) == []


def test_three_way_group_ordering_duplicates_and_ledger_input():
    a = metric('gmv', 'SUM(orders.amount)', ["orders.status = 'paid'"], source='yaml:b')
    b = metric('GMV', 'SUM(orders.amount)', ["orders.status = 'paid'"], source='yaml:a')
    c = metric('gmv', 'SUM(orders.amount)', [], source='yaml:c')
    z1 = metric('zeta', 'COUNT(*)', [], source='history')
    z2 = metric('zeta', 'COUNT(DISTINCT orders.id)', [], source='history')
    alone = metric('alpha', 'MAX(orders.amount)')
    conflicts = detect_conflicts([z1, a, a, b, c, z2, alone])
    assert [x.name for x in conflicts] == ['gmv', 'zeta']
    gmv = conflicts[0]
    assert gmv.sources == ['yaml:a', 'yaml:b', 'yaml:c'] and gmv.kinds == ['conditions']
    assert gmv.expressions == ["SUM(orders.amount) WHERE orders.status = 'paid'"] * 2 + ['SUM(orders.amount)']
    assert conflicts[1].kinds == ['expression'] and len(conflicts[1].definition_ids) == 2
    led = Ledger()
    for d in (z1, z2, alone):
        led.add(d)
    assert [x.name for x in detect_conflicts(led)] == ['zeta']
    assert detect_conflicts([]) == []


def test_conflict_defaults_and_planned_loader():
    assert Conflict('x') == Conflict(name='x', definition_ids=[], sources=[], expressions=[], kinds=[])
    with pytest.raises(NotImplementedYet):
        load_dbt_semantic_models('semantic_models.yml')
