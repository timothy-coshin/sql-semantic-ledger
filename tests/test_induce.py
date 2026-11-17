"""Tests for sql_semantic_ledger.induce: loading histories, candidates, independent support, names and ids."""
import json
import sqlite3

import pytest

from sql_semantic_ledger import EvidenceKind, Grade, Kind
from sql_semantic_ledger.induce import (MAX_NAME_LENGTH, QueryRecord, definition_id, definition_name, induce,
                                        load_history)
from sql_semantic_ledger.model import make_id

PAID_REVENUE = "SELECT SUM(o.amount) FROM orders o WHERE o.status = 'paid'"
PAID_REVENUE_BY_REGION = ('SELECT c.region, SUM(orders.amount) FROM orders JOIN customers c '
                          "ON orders.customer_id = c.id WHERE orders.status = 'paid' GROUP BY c.region")
METRIC = ('SUM(orders.amount)', ["orders.status = 'paid'"])


def find_one(ledger, kind, expression, conditions=()):
    matches = [d for d in ledger
               if d.kind == kind and d.expression == expression and sorted(d.conditions) == sorted(conditions)]
    assert len(matches) == 1, [(d.kind.value, d.expression, d.conditions) for d in ledger]
    return matches[0]


# ---------------------------------------------------------------------------------------------- support
def test_metric_used_by_two_different_queries_is_e1():
    led = induce([PAID_REVENUE, PAID_REVENUE_BY_REGION])
    metric = find_one(led, Kind.METRIC, *METRIC)
    assert metric.grade == Grade.E1 and metric.support == 2
    assert metric.name == 'sum_orders_amount_where_orders_status_eq_paid'
    assert metric.tables == ['orders'] and metric.columns == ['orders.amount', 'orders.status']
    assert [e.ref for e in metric.evidence] == ['q1', 'q2'] and metric.source == 'history'
    assert metric.evidence[0].detail == "SELECT SUM(orders.amount) FROM orders WHERE orders.status = 'paid'"
    assert find_one(led, Kind.MEASURE, 'SUM(orders.amount)').name == 'sum_orders_amount'
    assert find_one(led, Kind.FILTER, "orders.status = 'paid'").name == 'orders_status_eq_paid'
    assert all(d.grade == Grade.E1 for d in led) and len(led) == 3   # the join is used by one query only
    assert [d.kind for d in led] == [Kind.MEASURE, Kind.FILTER, Kind.METRIC]


def test_single_query_yields_nothing_or_e0_with_keep_unsupported():
    assert len(induce([PAID_REVENUE])) == 0
    led = induce([PAID_REVENUE], keep_unsupported=True)
    assert {d.kind for d in led} == {Kind.MEASURE, Kind.FILTER, Kind.METRIC}
    assert all(d.grade == Grade.E0 and d.support == 1 for d in led)
    assert find_one(led, Kind.METRIC, *METRIC).name == 'sum_orders_amount_where_orders_status_eq_paid'


def test_repeated_and_reparameterized_queries_count_once():
    rerun = [PAID_REVENUE, PAID_REVENUE, "select sum(amount) from orders where status = 'paid'"]
    assert len(induce(rerun)) == 0   # same skeleton, no explicit refs: one query
    assert len(induce([PAID_REVENUE, "SELECT SUM(amount) FROM orders WHERE status = 'shipped'"])) == 0
    led = induce(rerun, keep_unsupported=True)
    assert find_one(led, Kind.METRIC, *METRIC).meta['occurrences'] == 3


def test_code_records_count_like_history():
    sites = [QueryRecord(sql=PAID_REVENUE, kind=EvidenceKind.CODE, ref='src/api/orders.js:12'),
             QueryRecord(sql=PAID_REVENUE, kind='code', ref='src/ui/report.ts:40')]
    metric = find_one(induce(sites), Kind.METRIC, *METRIC)
    assert metric.grade == Grade.E1 and metric.sources == ['code'] and metric.source == 'code'
    assert [e.ref for e in metric.evidence] == ['src/api/orders.js:12', 'src/ui/report.ts:40']
    mixed = find_one(induce([PAID_REVENUE, sites[0]]), Kind.METRIC, *METRIC)
    assert mixed.grade == Grade.E1 and mixed.sources == ['code', 'history'] and mixed.source == 'code+history'
    assert len(induce([sites[0], sites[0]])) == 0   # one code site scanned twice is one site


def test_unparsable_and_non_select_records_are_skipped():
    led = induce(['SELEC broken', PAID_REVENUE, 'DROP TABLE orders', '', PAID_REVENUE_BY_REGION,
                  "SELECT 'unterminated", 'SELECT 1; SELECT 2'])
    metric = find_one(led, Kind.METRIC, *METRIC)
    assert [e.ref for e in metric.evidence] == ['q2', 'q5'] and metric.grade == Grade.E1


def test_same_definitions_through_aliases_ctes_and_or_order():
    led = induce(["WITH paid AS (SELECT * FROM orders WHERE status = 'paid') SELECT SUM(p.amount) FROM paid p",
                  PAID_REVENUE,
                  "SELECT COUNT(*) FROM orders WHERE status = 'paid' OR status = 'shipped'",
                  "SELECT SUM(x.amount) FROM orders x WHERE (x.status = 'shipped' OR 'paid' = x.status) AND x.amount > 0"])
    assert find_one(led, Kind.METRIC, *METRIC).support == 2
    assert find_one(led, Kind.FILTER, "orders.status = 'paid' OR orders.status = 'shipped'").grade == Grade.E1


def test_join_candidates():
    led = induce(['SELECT COUNT(*) FROM orders o JOIN customers c ON o.customer_id = c.id',
                  'SELECT c.region, SUM(x.amount) FROM customers AS c INNER JOIN orders AS x ON c.id = x.customer_id '
                  'GROUP BY c.region'])
    join = find_one(led, Kind.JOIN, 'customers.id = orders.customer_id')
    assert join.name == 'join_customers_orders' and join.grade == Grade.E1
    assert join.tables == ['customers', 'orders'] and join.columns == ['customers.id', 'orders.customer_id']


def test_one_off_conditions_do_not_hide_a_shared_metric():
    history = ["SELECT SUM(enrollment) FROM schools WHERE status <> 'Closed' AND county = 'Alameda'",
               "SELECT district, SUM(enrollment) FROM schools WHERE status <> 'Closed' AND charter = 1 GROUP BY district"]
    led = induce(history)
    metric = find_one(led, Kind.METRIC, 'SUM(schools.enrollment)', ["schools.status <> 'Closed'"])
    assert metric.grade == Grade.E1 and metric.name == 'sum_schools_enrollment_where_schools_status_ne_closed'
    assert not any('county' in c or 'charter' in c for d in led for c in d.conditions)
    everything = induce(history, keep_unsupported=True)
    full = find_one(everything, Kind.METRIC, 'SUM(schools.enrollment)',
                    ["schools.county = 'Alameda'", "schools.status <> 'Closed'"])
    assert full.grade == Grade.E0
    assert find_one(everything, Kind.METRIC, *metric_key(metric)).grade == Grade.E1


def metric_key(definition):
    return definition.expression, definition.conditions


def test_group_level_and_parameterized_conditions_are_not_definitions():
    led = induce(["SELECT customer_id, COUNT(*) AS n FROM orders WHERE status = 'paid' GROUP BY customer_id HAVING n > 5",
                  "SELECT region, COUNT(*) FROM orders WHERE status = 'paid' AND customer_id = ? GROUP BY region "
                  'HAVING COUNT(*) > 5'])
    assert [d.expression for d in led.find(kind=Kind.FILTER)] == ["orders.status = 'paid'"]
    metric = find_one(led, Kind.METRIC, 'COUNT(*)', ["orders.status = 'paid'"])
    assert metric.grade == Grade.E1 and metric.name == 'count_orders_where_orders_status_eq_paid'


def test_count_star_over_different_tables_are_different_definitions():
    led = induce(['SELECT COUNT(*) FROM orders', 'SELECT COUNT(1) FROM orders WHERE amount > 10',
                  'SELECT COUNT(*) FROM customers', "SELECT COUNT(*) FROM customers WHERE region = 'EU'"])
    counts = led.find(kind=Kind.MEASURE)
    assert sorted((d.name, tuple(d.tables), d.grade) for d in counts) == [
        ('count_customers', ('customers',), Grade.E1), ('count_orders', ('orders',), Grade.E1)]
    assert len({d.id for d in counts}) == 2
    assert {d.id for d in counts} == {definition_id(Kind.MEASURE, 'COUNT(*)', (), [t]) for t in ('customers', 'orders')}


def test_schema_hints_from_the_history_resolve_unqualified_columns():
    led = induce(["SELECT SUM(o.amount) FROM orders o JOIN customers c ON o.customer_id = c.id WHERE region = 'EU'",
                  "SELECT COUNT(*) FROM customers c WHERE c.region = 'EU'"])
    region = find_one(led, Kind.FILTER, "customers.region = 'EU'")
    assert region.support == 2 and region.grade == Grade.E1


# ---------------------------------------------------------------------------------------------- options and ledgers
def test_min_support_and_metrics_options():
    for bad in (0, -1, 1.5, True, '2'):
        with pytest.raises(ValueError):
            induce([PAID_REVENUE], min_support=bad)
    single = induce([PAID_REVENUE], min_support=1)
    assert len(single) == 3 and all(d.grade == Grade.E0 for d in single)   # kept, but E1 needs two supports
    no_metrics = induce([PAID_REVENUE, PAID_REVENUE_BY_REGION], metrics=False)
    assert not no_metrics.find(kind=Kind.METRIC) and no_metrics.find(kind=Kind.MEASURE)
    three = induce([PAID_REVENUE, PAID_REVENUE_BY_REGION], min_support=3)
    assert len(three) == 0


def test_record_validation():
    with pytest.raises(ValueError):
        induce([QueryRecord(sql=PAID_REVENUE, kind=EvidenceKind.MODEL)])
    with pytest.raises(ValueError):
        induce([QueryRecord(sql=PAID_REVENUE, kind='not-a-kind')])
    with pytest.raises(TypeError):
        induce([42])
    assert len(induce(iter([PAID_REVENUE, PAID_REVENUE_BY_REGION]))) == 3   # any iterable
    assert len(induce([])) == 0


def test_merging_into_an_existing_ledger_accumulates_support():
    led = induce([PAID_REVENUE], keep_unsupported=True)
    metric = find_one(led, Kind.METRIC, *METRIC)
    assert metric.grade == Grade.E0
    later = [QueryRecord(sql=PAID_REVENUE_BY_REGION, id='q100')]
    assert induce(later, ledger=led) is led   # support 1 in this call, but the definition already exists
    assert metric.grade == Grade.E1 and metric.support == 2
    assert [e.type for e in led.events].count('evidence') >= 1
    before = len(metric.evidence)
    induce(later, ledger=led)
    assert len(metric.evidence) == before   # the same evidence is not added twice


def test_a_query_run_again_in_a_later_history_still_counts_once():
    led = induce([PAID_REVENUE, 'SELECT COUNT(*) FROM customers'], keep_unsupported=True)
    metric = find_one(led, Kind.METRIC, *METRIC)
    count = find_one(led, Kind.MEASURE, 'COUNT(*)')
    keys = metric.meta['support_keys']
    assert list(keys) == ['1'] and len(keys['1']) == 1 and keys['1'][0].startswith('history|')
    # a later history (other positions, so other ids) runs the same queries again, one with another literal
    later = ['SELECT AVG(amount) FROM orders', 'SELECT COUNT(*) FROM customers', PAID_REVENUE,
             "select sum(amount) from orders where status = 'refunded'"]
    induce(later, ledger=led)
    assert (metric.support, metric.grade) == (1, Grade.E0) and (count.support, count.grade) == (1, Grade.E0)
    measure = find_one(led, Kind.MEASURE, 'SUM(orders.amount)')
    assert measure.support == 1                       # 'paid' and 'refunded' share one skeleton
    # code sites are told apart by their refs, also across calls
    site = QueryRecord(sql=PAID_REVENUE_BY_REGION, id='api.js:3:1', kind=EvidenceKind.CODE, ref='api.js:3')
    induce([site], ledger=led)
    induce([site], ledger=led)
    assert metric.support == 2 and metric.grade == Grade.E1 and metric.sources == ['code', 'history']
    assert len(metric.meta['support_keys']['1']) == 2


def test_evidence_follows_the_current_version_of_a_definition():
    led = induce([PAID_REVENUE, PAID_REVENUE_BY_REGION])
    measure = find_one(led, Kind.MEASURE, 'SUM(orders.amount)')
    led.revise(measure.id, note='recheck')   # same text, new version: old evidence no longer counts
    assert measure.grade == Grade.E0
    induce([PAID_REVENUE, PAID_REVENUE_BY_REGION], ledger=led)
    assert measure.version == 2 and measure.grade == Grade.E1
    revised = find_one(led, Kind.FILTER, "orders.status = 'paid'")
    led.revise(revised.id, expression="orders.status IN ('paid', 'settled')")
    induce([PAID_REVENUE, PAID_REVENUE_BY_REGION], ledger=led)
    assert revised.grade == Grade.E0   # the history supports the old text, not the revision


def test_output_is_deterministic():
    history = [PAID_REVENUE, PAID_REVENUE_BY_REGION, 'SELECT COUNT(*) FROM orders o JOIN customers c ON o.customer_id = c.id',
               "SELECT AVG(amount) FROM orders WHERE status = 'paid' AND amount > 0", 'SELECT COUNT(*) FROM orders']

    def snapshot():
        return [(d.id, d.name, d.kind, d.expression, d.conditions, d.tables, d.columns, [e.ref for e in d.evidence])
                for d in induce(history, keep_unsupported=True)]

    assert snapshot() == snapshot()


# ---------------------------------------------------------------------------------------------- ids and names
def test_definition_id_rules():
    assert definition_id('filter', "orders.status = 'paid'") == make_id(Kind.FILTER, "orders.status = 'paid'")
    assert definition_id('metric', 'SUM(orders.amount)', ["orders.status = 'paid'"], ['orders']) == \
        make_id(Kind.METRIC, 'SUM(orders.amount)', ["orders.status = 'paid'"])
    assert definition_id('measure', 'COUNT(*)', (), ['orders']) != definition_id('measure', 'COUNT(*)', (), ['customers'])


@pytest.mark.parametrize('kind, expression, conditions, tables, expected', [
    ('measure', 'SUM(orders.amount)', (), (), 'sum_orders_amount'),
    ('measure', 'COUNT(*)', (), ('orders',), 'count_orders'),
    ('measure', 'COUNT(*)', (), ('customers', 'orders'), 'count_customers_orders'),
    ('measure', 'COUNT(DISTINCT orders.customer_id)', (), (), 'count_distinct_orders_customer_id'),
    ('measure', "SUM(CASE WHEN orders.status = 'paid' THEN orders.amount ELSE 0 END)", (), (),
     'sum_case_when_orders_status_eq_paid_then_orders_amount_else_0_end'),
    ('filter', "orders.status = 'paid'", (), (), 'orders_status_eq_paid'),
    ('filter', "orders.status IN ('paid', 'shipped')", (), (), 'orders_status_in_paid_shipped'),
    ('filter', "NOT orders.status IN ('void')", (), (), 'orders_status_not_in_void'),
    ('filter', 'orders.amount BETWEEN 10 AND 100', (), (), 'orders_amount_between_10_and_100'),
    ('filter', "orders.note NOT LIKE '%gift%'", (), (), 'orders_note_not_like_gift'),
    ('filter', 'NOT orders.email IS NULL', (), (), 'orders_email_is_not_null'),
    ('filter', 'orders.coupon IS NULL', (), (), 'orders_coupon_is_null'),
    ('filter', 'orders.amount > -1', (), (), 'orders_amount_gt_minus_1'),
    ('filter', "orders.region = 'EU' OR orders.region = 'US'", (), (), 'orders_region_eq_eu_or_orders_region_eq_us'),
    ('filter', "customers.city = '서울'", (), (), 'customers_city_eq_서울'),
    ('join', 'customers.id = orders.customer_id', (), (), 'join_customers_orders'),
    ('metric', 'SUM(orders.amount)', ("orders.status = 'paid'",), (), 'sum_orders_amount_where_orders_status_eq_paid'),
    ('metric', 'COUNT(*)', ("orders.status = 'paid'", 'orders.amount > 0'), ('orders',),
     'count_orders_where_orders_status_eq_paid_and_orders_amount_gt_0'),
    # '*' inside a function: multiplication is 'times', COUNT(*) is 'all'
    ('measure', 'SUM(items.price * items.qty)', (), (), 'sum_items_price_times_items_qty'),
    ('measure', 'SUM("order items"."unit price" * "order items".qty)', (), (),
     'sum_order_items_unit_price_times_order_items_qty'),
    ('measure', 'ROUND(COUNT(*) * 1.0 / 2, 2)', (), (), 'round_count_all_times_1_0_div_2_2'),
])
def test_definition_names(kind, expression, conditions, tables, expected):
    assert definition_name(kind, expression, conditions, tables) == expected


def test_function_names_follow_the_written_sql():
    assert definition_name('filter', "STRFTIME('%Y', orders.created_at) = '2024'", dialect='sqlite') == \
        'strftime_y_orders_created_at_eq_2024'


def test_long_names_are_cut_and_stay_distinct():
    conditions = [f"orders.c{i} = 'value number {i}'" for i in range(6)]
    a = definition_name('metric', 'SUM(orders.amount)', conditions)
    b = definition_name('metric', 'SUM(orders.amount)', conditions[:-1] + ["orders.c5 = 'other'"])
    assert len(a) <= MAX_NAME_LENGTH and len(b) <= MAX_NAME_LENGTH and a != b
    assert a.startswith('sum_orders_amount_where_orders_c0_eq_value_number_0')


# ---------------------------------------------------------------------------------------------- loading
def test_load_history_sql(tmp_path):
    path = tmp_path / 'history.sql'
    path.write_text("-- daily revenue\nSELECT SUM(amount) FROM orders WHERE note = 'a;b';\n\nSELECT 1;\n;\n",
                    encoding='utf-8')
    records = load_history(path)
    assert [r.id for r in records] == ['q1', 'q2'] and all(r.kind == EvidenceKind.HISTORY for r in records)
    assert records[0].sql.endswith("note = 'a;b'") and records[1].sql == 'SELECT 1'
    assert load_history(str(path))[0].sql == records[0].sql
    bom = tmp_path / 'excel.sql'
    bom.write_text('SELECT 1;SELECT 2', encoding='utf-8-sig')   # a byte-order mark is not part of the SQL
    assert [r.sql for r in load_history(bom)] == ['SELECT 1', 'SELECT 2']


def test_load_history_jsonl_and_induce(tmp_path):
    path = tmp_path / 'history.jsonl'
    rows = [{'sql': PAID_REVENUE, 'id': 17, 'user': 'analyst_a', 'at': '2024-05-01T10:00:00'}, None,
            {'sql': PAID_REVENUE_BY_REGION}, {'sql': PAID_REVENUE, 'kind': 'code', 'ref': 'src/api/orders.js:12'}]
    path.write_text('\n'.join('' if r is None else json.dumps(r) for r in rows) + '\n', encoding='utf-8')
    records = load_history(path)
    assert [(r.id, r.kind, r.ref, r.user, r.at) for r in records] == [
        ('17', EvidenceKind.HISTORY, None, 'analyst_a', '2024-05-01T10:00:00'),
        ('q2', EvidenceKind.HISTORY, None, None, None),
        ('q3', EvidenceKind.CODE, 'src/api/orders.js:12', None, None)]
    metric = find_one(induce(records), Kind.METRIC, *METRIC)
    assert metric.support == 3 and metric.sources == ['code', 'history']
    assert metric.evidence[0].detail.endswith('(user=analyst_a, at=2024-05-01T10:00:00)')


@pytest.mark.parametrize('line, message', [
    ('{"sql": "SELECT 1"', 'invalid JSON'),
    ('["SELECT 1"]', 'JSON object'),
    ('{"id": 3}', "'sql'"),
    ('{"sql": "   "}', "'sql'"),
    ('{"sql": "SELECT 1", "kind": "model"}', "'kind'"),
])
def test_load_history_jsonl_errors_name_the_line(tmp_path, line, message):
    path = tmp_path / 'bad.jsonl'
    path.write_text('{"sql": "SELECT 1"}\n' + line + '\n', encoding='utf-8')
    with pytest.raises(ValueError, match=message) as info:
        load_history(path)
    assert 'bad.jsonl:2' in str(info.value)


def test_load_history_rejects_other_formats(tmp_path):
    path = tmp_path / 'history.csv'
    path.write_text('sql\nSELECT 1\n', encoding='utf-8')
    with pytest.raises(ValueError, match='unsupported'):
        load_history(path)
    with pytest.raises(FileNotFoundError):
        load_history(tmp_path / 'missing.sql')


# ---------------------------------------------------------------------------------------------- executable on a database
SHOP = """
    CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, region TEXT);
    CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers (id), amount REAL,
                         status TEXT, note TEXT);
    INSERT INTO customers VALUES (1, 'Ann', 'EU'), (2, 'Bob', 'US'), (3, 'Alice', 'APAC');
    INSERT INTO orders VALUES (1, 1, 10, 'paid', NULL), (2, 1, 250, 'paid', 'gift'), (3, 2, 5, 'void', NULL),
                              (4, 3, 70, 'paid', 'rush'), (5, 2, 30, 'paid', NULL);
"""


def definition_query(definition, joins):
    """SELECT that evaluates a definition, joining its tables through the ledger's JOIN definitions."""
    if definition.kind == Kind.JOIN:
        first, second = definition.tables
        return f'SELECT COUNT(*) FROM {first} JOIN {second} ON {definition.expression}'
    first, *others = definition.tables
    source = first
    for other in others:
        (join,) = [j for j in joins if set(j.tables) == {first, other}]
        source += f' JOIN {other} ON {join.expression}'
    if definition.kind == Kind.FILTER:
        return f'SELECT COUNT(*) FROM {source} WHERE {definition.expression}'
    where = f" WHERE {' AND '.join(definition.conditions)}" if definition.conditions else ''
    return f'SELECT {definition.expression} FROM {source}{where}'


def test_induced_definitions_are_executable_on_the_database(tmp_path):
    conn = sqlite3.connect(str(tmp_path / 'shop.sqlite'))
    conn.executescript(SHOP)
    history = [
        "SELECT SUM(o.amount) FROM orders o WHERE o.status = 'paid' AND o.amount BETWEEN 1 AND 100",
        'SELECT c.region, SUM(o.amount) FROM orders o JOIN customers c ON o.customer_id = c.id '
        "WHERE o.status = 'paid' AND o.amount BETWEEN 1 AND 100 GROUP BY c.region",
        "SELECT COUNT(*) FROM customers c JOIN orders o ON c.id = o.customer_id WHERE c.region IN ('EU', 'APAC') "
        'AND o.note IS NOT NULL',
        "SELECT COUNT(*) FROM customers WHERE region IN ('APAC', 'EU') AND name LIKE 'A%'",
        'SELECT AVG(amount) FROM orders WHERE note IS NOT NULL',
        "SELECT MAX(amount) FROM orders o WHERE o.note IS NOT NULL AND o.status = 'paid'",
    ]
    led = induce(history, dialect='sqlite')
    joins = led.find(kind=Kind.JOIN)
    assert [j.expression for j in joins] == ['customers.id = orders.customer_id']
    assert {d.kind for d in led} == {Kind.MEASURE, Kind.FILTER, Kind.JOIN, Kind.METRIC}
    assert all(d.grade == Grade.E1 for d in led)
    for definition in led:
        conn.execute(definition_query(definition, joins)).fetchall()   # raises if a fragment is not valid SQL
    metric = find_one(led, Kind.METRIC, 'SUM(orders.amount)',
                      ['orders.amount BETWEEN 1 AND 100', "orders.status = 'paid'"])
    assert conn.execute(definition_query(metric, joins)).fetchone() == conn.execute(history[0]).fetchone() == (110.0,)
    region = find_one(led, Kind.FILTER, "customers.region IN ('APAC', 'EU')")
    assert conn.execute(definition_query(region, joins)).fetchone() == (2,)
