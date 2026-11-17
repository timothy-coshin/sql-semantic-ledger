"""Tests for checks: data checks (E2) and counterexample checks (E4) on tiny SQLite databases."""
import sqlite3

import pytest

from sql_semantic_ledger import Definition, Evidence, EvidenceKind, Grade, Kind, Ledger
from sql_semantic_ledger.checks import CheckResult, counterexample_checks, run_checks

SCHEMA = """
CREATE TABLE regions (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE customers (id INTEGER PRIMARY KEY, region_id INTEGER, segment TEXT, is_test INTEGER);
CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, amount REAL, status TEXT);
CREATE TABLE "order items" (order_id INTEGER, sku TEXT, qty INTEGER);
CREATE TABLE employees (id INTEGER PRIMARY KEY, manager_id INTEGER, name TEXT);
INSERT INTO regions VALUES (1, 'EU'), (2, 'US');
INSERT INTO customers VALUES (1, 1, 'retail', 0), (2, 2, 'retail', 0), (3, 1, 'b2b', 1);
INSERT INTO orders VALUES (1, 1, 10.0, 'paid'), (2, 1, 20.0, 'paid'), (3, 2, 5.0, 'refunded'), (4, 3, 7.5, 'paid');
INSERT INTO "order items" VALUES (1, 'a', 1), (1, 'b', 2), (2, 'a', 1);
INSERT INTO employees VALUES (1, NULL, 'boss'), (2, 1, 'ann'), (3, 1, 'bob');
"""


@pytest.fixture
def conn(tmp_path):
    connection = sqlite3.connect(str(tmp_path / 'shop.sqlite'))
    connection.executescript(SCHEMA)
    yield connection
    connection.close()


def _supported(d, refs=('q1', 'q2')):
    """Give ``d`` independent support from past queries (two refs make it E1)."""
    d.evidence += [Evidence(EvidenceKind.HISTORY, ref=r) for r in refs]
    return d


def _ledger(*definitions):
    led = Ledger()
    for d in definitions:
        led.add(d)
    return led


def _join(expression, tables):
    return _supported(Definition(name='join_' + '_'.join(sorted(tables)), kind=Kind.JOIN, expression=expression,
                                 tables=list(tables)))


ORDERS_CUSTOMERS = 'customers.id = orders.customer_id'
CUSTOMERS_REGIONS = 'customers.region_id = regions.id'


def _by_name(results):
    return {r.name: r for r in results}


# ------------------------------------------------------------------------------------------- single-table checks
def test_passing_checks_raise_e1_to_e2_and_record_evidence(conn):
    metric = _supported(Definition(name='paid_revenue', kind=Kind.METRIC, expression='SUM(orders.amount)',
                                   conditions=["orders.status = 'paid'"], tables=['orders'],
                                   columns=['orders.amount', 'orders.status']))
    paid = _supported(Definition(name='paid', kind=Kind.FILTER, expression="orders.status = 'paid'", tables=['orders']))
    led = _ledger(metric, paid)
    assert metric.grade == Grade.E1

    results = run_checks(led, conn)

    assert [(r.definition_id, r.name) for r in results] == [
        (metric.id, 'columns_exist'), (metric.id, 'executes'), (paid.id, 'columns_exist'), (paid.id, 'selects_rows')]
    assert all(isinstance(r, CheckResult) and r.passed for r in results)
    assert 'returned 37.5' in results[1].detail and "WHERE (orders.status = 'paid')" in results[1].detail
    assert 'selects 3 rows' in results[3].detail
    assert metric.grade == Grade.E2 and paid.grade == Grade.E2
    checks = metric.current_evidence(EvidenceKind.DATA_CHECK)
    assert [(e.ref, e.passed, e.version) for e in checks] == [('columns_exist', True, 1), ('executes', True, 1)]
    assert [e.type for e in led.events].count('evidence') == 4


def test_failed_check_falsifies(conn):
    cancelled = _supported(Definition(name='cancelled_revenue', kind=Kind.METRIC, expression='SUM(orders.amount)',
                                      conditions=["orders.status = 'cancelled'"], tables=['orders']))
    nothing = _supported(Definition(name='never', kind=Kind.FILTER, expression="orders.status = 'lost'", tables=['orders']))
    led = _ledger(cancelled, nothing)
    results = _by_name(run_checks(led, conn, [cancelled.id]))
    assert not results['executes'].passed and 'returned NULL' in results['executes'].detail
    assert cancelled.falsified and cancelled.grade == Grade.E0
    assert not run_checks(led, conn, [nothing.id])[1].passed and nothing.grade == Grade.E0


def test_count_over_no_rows_fails_like_the_filter_with_its_conditions(conn):
    conn.execute('CREATE TABLE archive (id INTEGER)')
    lost = _supported(Definition(name='lost_orders', kind=Kind.METRIC, expression='COUNT(*)',
                                 conditions=["orders.status = 'lost'"], tables=['orders']))
    paid = _supported(Definition(name='paid_orders', kind=Kind.METRIC, expression='COUNT(*)',
                                 conditions=["orders.status = 'paid'"], tables=['orders']))
    empty = _supported(Definition(name='archived', kind=Kind.MEASURE, expression='COUNT(archive.id)',
                                  tables=['archive']))
    nothing = _supported(Definition(name='lost', kind=Kind.FILTER, expression="orders.status = 'lost'",
                                    tables=['orders']))
    results = {(r.definition_id, r.name): r for r in run_checks(_ledger(lost, paid, empty, nothing), conn)}
    for d in (lost, empty):
        executes = results[(d.id, 'executes')]
        assert not executes.passed and 'returned 0 but reads no rows' in executes.detail
        assert d.falsified and d.grade == Grade.E0
    assert not results[(nothing.id, 'selects_rows')].passed and nothing.grade == Grade.E0
    assert results[(paid.id, 'executes')].detail.startswith('returned 3 ') and paid.grade == Grade.E2


def test_missing_columns_and_tables(conn):
    typo = _supported(Definition(name='typo', kind=Kind.MEASURE, expression='SUM(orders.amt)', tables=['orders']))
    gone = _supported(Definition(name='refunds', kind=Kind.MEASURE, expression='SUM(refunds.amount)', tables=['refunds']))
    listed = _supported(Definition(name='listed', kind=Kind.DIMENSION, expression='customers.segment',
                                   tables=['customers'], columns=['customers.segment', 'customers.tier']))
    led = _ledger(typo, gone, listed)
    results = run_checks(led, conn)
    by_def = {(r.definition_id, r.name): r for r in results}
    assert by_def[(typo.id, 'columns_exist')].detail == 'missing columns: orders.amt'
    assert not by_def[(typo.id, 'executes')].passed and 'no such column' in by_def[(typo.id, 'executes')].detail
    assert by_def[(gone.id, 'columns_exist')].detail == 'missing tables: refunds'
    assert 'missing columns: customers.tier' in by_def[(listed.id, 'columns_exist')].detail
    assert all(d.grade == Grade.E0 for d in (typo, gone, listed))


def test_dimension_gets_only_columns_exist(conn):
    segment = _supported(Definition(name='segment', kind=Kind.DIMENSION, expression='customers.segment',
                                    tables=['customers']))
    results = run_checks(_ledger(segment), conn)
    assert [r.name for r in results] == ['columns_exist'] and results[0].passed
    assert segment.grade == Grade.E2


def test_unqualified_and_quoted_names(conn):
    unqualified = _supported(Definition(name='amount', kind=Kind.MEASURE, expression='SUM(amount)', tables=['orders']))
    unknown = _supported(Definition(name='nope', kind=Kind.MEASURE, expression='SUM(nope)', tables=['orders']))
    quoted = _supported(Definition(name='qty', kind=Kind.MEASURE, expression='SUM("order items".qty)',
                                   tables=['order items'], columns=['order items.qty']))
    led = _ledger(unqualified, unknown, quoted)
    by_def = {(r.definition_id, r.name): r for r in run_checks(led, conn)}
    assert by_def[(unqualified.id, 'columns_exist')].passed and by_def[(unqualified.id, 'executes')].passed
    assert by_def[(unknown.id, 'columns_exist')].detail == 'missing columns: nope'
    assert by_def[(quoted.id, 'executes')].passed and 'FROM "order items"' in by_def[(quoted.id, 'executes')].detail
    assert 'returned 4' in by_def[(quoted.id, 'executes')].detail


QUOTED_SCHEMA = """
CREATE TABLE "Order Lines" (order_id INTEGER, "Unit Price" REAL, qty INTEGER);
CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT);
INSERT INTO orders VALUES (1, 'paid'), (2, 'open');
INSERT INTO "Order Lines" VALUES (1, 2.5, 2), (1, 1.0, 1), (2, 3.0, 4);
"""


@pytest.fixture
def quoted_conn():
    connection = sqlite3.connect(':memory:')
    connection.executescript(QUOTED_SCHEMA)
    yield connection
    connection.close()


def test_names_written_as_sql_text_like_induce_writes_them(quoted_conn):
    """Definition.tables/columns hold SQL text, quoted where needed (``"Order Lines"``), as induce stores them."""
    lines = '"Order Lines"'
    join = _join(f'{lines}.order_id = orders.id', [lines, 'orders'])
    revenue = _supported(Definition(name='paid_line_revenue', kind=Kind.METRIC,
                                    expression=f'SUM({lines}."Unit Price" * {lines}.qty)',
                                    conditions=["orders.status = 'paid'"], tables=[lines, 'orders'],
                                    columns=[f'{lines}."Unit Price"', f'{lines}.qty', 'orders.status']))
    pricey = _supported(Definition(name='pricey', kind=Kind.FILTER, expression=f'{lines}."Unit Price" > 2',
                                   tables=[lines], columns=[f'{lines}."Unit Price"']))
    led = _ledger(join, revenue, pricey)
    by_def = {(r.definition_id, r.name): r for r in run_checks(led, quoted_conn)}
    assert all(r.passed for r in by_def.values()), [r.detail for r in by_def.values() if not r.passed]
    assert all(d.grade == Grade.E2 for d in (join, revenue, pricey))
    columns = by_def[(revenue.id, 'columns_exist')].detail
    assert columns == 'tables "Order Lines", orders; columns "Order Lines"."Unit Price", "Order Lines".qty, orders.status'
    executes = by_def[(revenue.id, 'executes')].detail
    assert 'returned 6' in executes and 'FROM "Order Lines" JOIN orders ON "Order Lines".order_id = orders.id' in executes
    assert 'selects 2 rows' in by_def[(pricey.id, 'selects_rows')].detail
    assert 'not unique: "Order Lines"(order_id)' in by_def[(join.id, 'key_cardinality')].detail


def test_quoted_names_match_case_insensitively_and_missing_ones_are_named_as_sql(quoted_conn):
    lower = _supported(Definition(name='qty', kind=Kind.MEASURE, expression='SUM("order lines".qty)',
                                  tables=['"order lines"'], columns=['"order lines".qty']))
    typo = _supported(Definition(name='cost', kind=Kind.MEASURE, expression='SUM("Order Lines"."Unit Cost")',
                                 tables=['"Order Lines"'], columns=['"Order Lines"."Unit Cost"']))
    gone = _supported(Definition(name='gone', kind=Kind.MEASURE, expression='COUNT(*)', tables=['"Old Lines"']))
    by_def = {(r.definition_id, r.name): r for r in run_checks(_ledger(lower, typo, gone), quoted_conn)}
    assert by_def[(lower.id, 'columns_exist')].passed and 'returned 7' in by_def[(lower.id, 'executes')].detail
    assert by_def[(typo.id, 'columns_exist')].detail == 'missing columns: "Order Lines"."Unit Cost"'
    assert by_def[(gone.id, 'columns_exist')].detail == 'missing tables: "Old Lines"'
    assert lower.grade == Grade.E2 and typo.grade == Grade.E0 and gone.grade == Grade.E0


def test_induced_definitions_with_quoted_names_reach_e2(quoted_conn):
    from sql_semantic_ledger.induce import induce

    led = induce([
        'SELECT SUM(l."Unit Price" * l.qty) FROM "Order Lines" l JOIN orders o ON o.id = l.order_id '
        "WHERE o.status = 'paid'",
        'SELECT orders.id, SUM(x."Unit Price" * x.qty) FROM "Order Lines" AS x JOIN orders ON orders.id = x.order_id '
        "WHERE orders.status = 'paid' GROUP BY orders.id",
    ])
    assert {d.kind for d in led} == {Kind.MEASURE, Kind.FILTER, Kind.JOIN, Kind.METRIC}
    results = run_checks(led, quoted_conn)
    assert all(r.passed for r in results), [r.detail for r in results if not r.passed]
    assert all(d.grade == Grade.E2 for d in led)


def test_case_folding_dialect_keeps_quoted_case(conn):
    """PostgreSQL folds unquoted names to lower case: ``Orders`` is orders, ``"Orders"`` keeps its case."""
    folded = _supported(Definition(name='folded', kind=Kind.MEASURE, expression='SUM(Orders.amount)',
                                   tables=['Orders'], columns=['Orders.Amount']))
    exact = _supported(Definition(name='exact', kind=Kind.MEASURE, expression='SUM("Orders".amount)',
                                  tables=['"Orders"'], columns=['"Orders".amount']))
    by_def = {(r.definition_id, r.name): r for r in run_checks(_ledger(folded, exact), _Wrapped(conn),
                                                                  dialect='postgres')}
    assert 'FROM orders' in by_def[(folded.id, 'executes')].detail
    assert 'tables orders; columns orders.amount' == by_def[(folded.id, 'columns_exist')].detail
    assert 'FROM "Orders"' in by_def[(exact.id, 'executes')].detail   # SQLite underneath ignores the case
    assert by_def[(exact.id, 'columns_exist')].detail.startswith('tables "Orders";')


def test_reserved_word_table_is_quoted():
    connection = sqlite3.connect(':memory:')
    connection.executescript('CREATE TABLE "group" (id INTEGER, size INTEGER); INSERT INTO "group" VALUES (1, 3);')
    total = _supported(Definition(name='size', kind=Kind.MEASURE, expression='SUM("group".size)', tables=['group']))
    results = _by_name(run_checks(_ledger(total), connection))
    assert results['columns_exist'].passed and results['executes'].passed
    assert 'FROM "group"' in results['executes'].detail
    connection.close()


def test_subquery_tables_are_not_added_to_from(conn):
    real = _supported(Definition(
        name='real_customer_orders', kind=Kind.FILTER,
        expression='orders.customer_id IN (SELECT customers.id FROM customers WHERE customers.is_test = 0)',
        tables=['orders', 'customers'], columns=['orders.customer_id', 'customers.id', 'customers.is_test']))
    results = _by_name(run_checks(_ledger(real), conn))   # no JOIN definition in the ledger
    assert results['columns_exist'].passed and 'tables orders, customers' in results['columns_exist'].detail
    assert results['selects_rows'].passed and 'selects 3 rows' in results['selects_rows'].detail
    assert 'FROM orders WHERE' in results['selects_rows'].detail


# ------------------------------------------------------------------------------------------- joins
def test_join_checks_and_cardinality(conn):
    good = _join(ORDERS_CUSTOMERS, ['customers', 'orders'])
    many_to_many = _join('"order items".order_id = orders.customer_id', ['order items', 'orders'])
    led = _ledger(good, many_to_many)
    by_def = {(r.definition_id, r.name): r for r in run_checks(led, conn)}
    assert by_def[(good.id, 'joins_rows')].passed and 'join returns 4 rows' in by_def[(good.id, 'joins_rows')].detail
    cardinality = by_def[(good.id, 'key_cardinality')]
    assert cardinality.passed and 'unique: customers(id)' in cardinality.detail
    assert 'not unique: orders(customer_id) has 1 repeated key' in cardinality.detail
    assert good.grade == Grade.E2
    assert by_def[(many_to_many.id, 'joins_rows')].passed
    bad = by_def[(many_to_many.id, 'key_cardinality')]
    assert not bad.passed and bad.detail.startswith('neither side is unique')
    assert many_to_many.grade == Grade.E0


def test_join_without_matches_fails(conn):
    nonsense = _join('customers.id = orders.amount', ['customers', 'orders'])
    results = _by_name(run_checks(_ledger(nonsense), conn))
    assert not results['joins_rows'].passed and 'join returns 0 rows' in results['joins_rows'].detail
    assert nonsense.falsified


def test_self_join(conn):
    reports_to = _join('employees.id = employees.manager_id', ['employees'])
    results = _by_name(run_checks(_ledger(reports_to), conn))
    assert results['joins_rows'].passed and 'join returns 2 rows' in results['joins_rows'].detail
    assert 'ledger_l.id = ledger_r.manager_id' in results['joins_rows'].detail
    assert results['key_cardinality'].passed and 'unique: employees(id)' in results['key_cardinality'].detail


def test_unusable_join_condition_fails_both_join_checks(conn):
    odd = _supported(Definition(name='odd', kind=Kind.JOIN, expression='orders.customer_id > 0', tables=['orders']))
    results = _by_name(run_checks(_ledger(odd), conn))
    assert not results['joins_rows'].passed and not results['key_cardinality'].passed
    assert 'must equate columns of two tables' in results['joins_rows'].detail


def test_multi_table_metric_is_connected_through_join_chain(conn):
    eu_revenue = _supported(Definition(name='eu_revenue', kind=Kind.METRIC, expression='SUM(orders.amount)',
                                       conditions=["regions.name = 'EU'"], tables=['orders', 'regions']))
    led = _ledger(eu_revenue, _join(ORDERS_CUSTOMERS, ['customers', 'orders']),
                  _join(CUSTOMERS_REGIONS, ['customers', 'regions']))
    executes = _by_name(run_checks(led, conn, [eu_revenue.id]))['executes']
    assert executes.passed and 'returned 37.5' in executes.detail
    assert ('FROM orders JOIN customers ON customers.id = orders.customer_id '
            'JOIN regions ON customers.region_id = regions.id') in executes.detail


def test_multi_table_definition_without_join_fails_clearly(conn):
    eu_revenue = _supported(Definition(name='eu_revenue', kind=Kind.METRIC, expression='SUM(orders.amount)',
                                       conditions=["regions.name = 'EU'"], tables=['orders', 'regions']))
    results = _by_name(run_checks(_ledger(eu_revenue), conn))
    assert results['columns_exist'].passed
    assert not results['executes'].passed
    assert results['executes'].detail == 'no JOIN definition in the ledger connects regions with orders'
    assert eu_revenue.grade == Grade.E0


def test_filter_on_joined_tables(conn):
    test_accounts = _supported(Definition(name='test_account_orders', kind=Kind.FILTER,
                                          expression='customers.is_test = 1', tables=['orders', 'customers']))
    led = _ledger(test_accounts, _join(ORDERS_CUSTOMERS, ['customers', 'orders']))
    selects = _by_name(run_checks(led, conn, [test_accounts.id]))['selects_rows']
    assert selects.passed and 'selects 1 row ' in selects.detail


def test_joins_are_checked_first_and_falsified_joins_are_not_used(conn):
    revenue_by_segment = _supported(Definition(name='retail_revenue', kind=Kind.METRIC, expression='SUM(orders.amount)',
                                               conditions=["customers.segment = 'retail'"],
                                               tables=['orders', 'customers']))
    wrong = _join('customers.id = orders.amount', ['customers', 'orders'])
    wrong.evidence.append(Evidence(EvidenceKind.HISTORY, ref='q3'))      # better supported, but wrong
    led = _ledger(revenue_by_segment, wrong)
    results = run_checks(led, conn)
    assert [r.definition_id for r in results][:2] == [revenue_by_segment.id] * 2    # input order is kept
    assert wrong.falsified
    executes = _by_name(r for r in results if r.definition_id == revenue_by_segment.id)['executes']
    assert not executes.passed and 'no JOIN definition' in executes.detail

    right = _join(ORDERS_CUSTOMERS, ['customers', 'orders'])
    led.add(right)
    led.revise(revenue_by_segment.id, note='recheck with the right join')   # a new version starts without evidence
    assert not revenue_by_segment.falsified
    executes = _by_name(run_checks(led, conn, [revenue_by_segment.id, right.id]))['executes']
    assert executes.passed and 'ON customers.id = orders.customer_id' in executes.detail
    assert 'returned 35' in executes.detail


def test_expired_join_is_not_used(conn):
    metric = _supported(Definition(name='retail_revenue', kind=Kind.METRIC, expression='SUM(orders.amount)',
                                   conditions=["customers.segment = 'retail'"], tables=['orders', 'customers']))
    old = _join(ORDERS_CUSTOMERS, ['customers', 'orders'])
    old.valid_until = '2000-01-01T00:00:00+00:00'
    assert not _by_name(run_checks(_ledger(metric, old), conn, [metric.id]))['executes'].passed


# ------------------------------------------------------------------------------------------- inputs and connections
def test_definition_ids_select_and_validate(conn):
    a = _supported(Definition(name='a', kind=Kind.FILTER, expression="orders.status = 'paid'", tables=['orders']))
    b = _supported(Definition(name='b', kind=Kind.FILTER, expression="orders.status = 'refunded'", tables=['orders']))
    led = _ledger(a, b)
    assert {r.definition_id for r in run_checks(led, conn, a.id)} == {a.id}       # a single id string is accepted
    assert not b.current_evidence(EvidenceKind.DATA_CHECK)
    with pytest.raises(KeyError):
        run_checks(led, conn, [b.id, 'no-such-id'])
    assert not b.current_evidence(EvidenceKind.DATA_CHECK)                        # nothing ran
    assert run_checks(led, conn, []) == []
    assert len(run_checks(led, conn, [b.id, b.id])) == 2


def test_unknown_dialect_and_bad_connections(conn):
    d = _supported(Definition(name='a', kind=Kind.FILTER, expression="orders.status = 'paid'", tables=['orders']))
    led = _ledger(d)
    with pytest.raises(ValueError):
        run_checks(led, conn, dialect='no-such-dialect')
    with pytest.raises(TypeError):
        run_checks(led, 'shop.sqlite')
    closed = sqlite3.connect(':memory:')
    closed.close()
    with pytest.raises(RuntimeError):
        run_checks(led, closed)
    assert not d.evidence[2:]                                                      # nothing was recorded


def test_empty_ledger(conn):
    assert run_checks(Ledger(), conn) == []


class _Wrapped:
    """A DB-API connection that is not a sqlite3.Connection (exercises the generic probe and rollback)."""

    def __init__(self, inner):
        self.inner = inner
        self.rollbacks = 0

    def cursor(self):
        return self.inner.cursor()

    def rollback(self):
        self.rollbacks += 1
        self.inner.rollback()


def test_generic_connection_uses_probe_and_rolls_back_after_errors(conn):
    wrapped = _Wrapped(conn)
    good = _supported(Definition(name='paid', kind=Kind.FILTER, expression="orders.status = 'paid'", tables=['orders']))
    broken = _supported(Definition(name='broken', kind=Kind.MEASURE, expression='SUM(orders.nope)', tables=['orders']))
    missing = _supported(Definition(name='missing', kind=Kind.MEASURE, expression='SUM(ghost.x)', tables=['ghost']))
    led = _ledger(good, broken, missing)
    by_def = {(r.definition_id, r.name): r for r in run_checks(led, wrapped, dialect='postgres')}
    assert by_def[(good.id, 'columns_exist')].passed and by_def[(good.id, 'selects_rows')].passed
    assert by_def[(broken.id, 'columns_exist')].detail == 'missing columns: orders.nope'
    assert by_def[(missing.id, 'columns_exist')].detail == 'missing tables: ghost'
    assert wrapped.rollbacks >= 3                    # failed probe for ghost and the two failed SELECTs


# ------------------------------------------------------------------------------------------- unsafe fragments
def _rows(conn, table):
    return conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]


@pytest.mark.parametrize('kind, expression, conditions, why', [
    (Kind.MEASURE, 'SUM(orders.amount); DELETE FROM orders', [], 'statement separator'),
    (Kind.FILTER, "orders.status = 'paid'", ["orders.status = 'x') OR (1 = 1"], 'unbalanced'),
    (Kind.FILTER, 'DELETE FROM orders', [], 'DELETE statement'),
])
def test_unsafe_fragments_are_refused(conn, kind, expression, conditions, why):
    d = _supported(Definition(name='unsafe', kind=kind, expression=expression, conditions=conditions, tables=['orders']))
    refused = run_checks(_ledger(d), conn)[-1]
    assert not refused.passed and refused.detail.startswith('refused to run') and why in refused.detail
    assert d.falsified and _rows(conn, 'orders') == 4


def test_line_comment_cannot_swallow_the_check_query(conn):
    paid = _supported(Definition(name='paid', kind=Kind.MEASURE, expression='COUNT(*) -- every order',
                                 conditions=["orders.status = 'paid' -- not refunded"], tables=['orders']))
    executes = _by_name(run_checks(_ledger(paid), conn))['executes']
    assert executes.passed and executes.detail.startswith('returned 3 ')     # not 1 from a bare SELECT COUNT(*)


def test_unsafe_join_is_refused_and_never_used_to_connect(conn):
    bad = _join('customers.id = orders.customer_id; DELETE FROM customers', ['customers', 'orders'])
    metric = _supported(Definition(name='retail_revenue', kind=Kind.METRIC, expression='SUM(orders.amount)',
                                   conditions=["customers.segment = 'retail'"], tables=['orders', 'customers']))
    led = _ledger(bad, metric)
    executes = _by_name(run_checks(led, conn, [metric.id]))['executes']      # the bad join is not checked yet
    assert not executes.passed and 'no JOIN definition' in executes.detail
    joins_rows = _by_name(run_checks(led, conn, [bad.id]))['joins_rows']
    assert not joins_rows.passed and 'statement separator' in joins_rows.detail
    assert _rows(conn, 'customers') == 3


def test_unsafe_counterexample_is_refused(conn):
    led = Ledger()
    metric = _certified_metric(led)
    with pytest.raises(ValueError, match='statement separator'):
        counterexample_checks(led, conn, metric.id, ['orders.amount < 0; DROP TABLE orders'])
    assert _rows(conn, 'orders') == 4 and metric.grade == Grade.E3


# ------------------------------------------------------------------------------------------- counterexamples
def _certified_metric(led):
    metric = _supported(Definition(name='paid_revenue', kind=Kind.METRIC, expression='SUM(orders.amount)',
                                   conditions=["orders.status = 'paid'"], tables=['orders']))
    led.add(metric)
    led.certify(metric.id, by='analyst', note='matches the finance report')
    assert metric.grade == Grade.E3
    return metric


def test_counterexample_checks_pass_to_e4(conn):
    led = Ledger()
    metric = _certified_metric(led)
    results = counterexample_checks(led, conn, metric.id, ['orders.amount < 0', "orders.status = 'paid' AND orders.amount IS NULL"])
    assert [r.passed for r in results] == [True, True]
    assert results[0].name == 'orders.amount < 0' and results[0].detail.startswith('0 violating rows')
    assert metric.grade == Grade.E4
    evidence = metric.current_evidence(EvidenceKind.COUNTEREXAMPLE)
    assert [e.ref for e in evidence] == ['orders.amount < 0', "orders.status = 'paid' AND orders.amount IS NULL"]


def test_counterexample_found_falsifies(conn):
    led = Ledger()
    metric = _certified_metric(led)
    results = counterexample_checks(led, conn, metric.id, 'orders.amount > 15')     # a single predicate string
    assert len(results) == 1 and not results[0].passed and results[0].detail.startswith('1 violating row ')
    assert metric.falsified and metric.grade == Grade.E0


def test_counterexample_over_joined_tables(conn):
    led = Ledger()
    metric = _certified_metric(led)
    led.add(_join(ORDERS_CUSTOMERS, ['customers', 'orders']))
    results = counterexample_checks(led, conn, metric.id, ["orders.status = 'paid' AND customers.is_test = 1"])
    assert not results[0].passed and 'JOIN customers ON customers.id = orders.customer_id' in results[0].detail


def test_counterexample_on_a_join_uses_its_own_condition(conn):
    led = Ledger()
    own = _join(ORDERS_CUSTOMERS, ['customers', 'orders'])
    other = _supported(Definition(name='other', kind=Kind.JOIN, expression='customers.id = orders.id',
                                  tables=['customers', 'orders']), refs=('q1', 'q2', 'q3', 'q4'))
    led.add(own)
    led.add(other)
    results = counterexample_checks(led, conn, own.id, ['orders.amount <= 0'])
    assert results[0].passed and 'ON customers.id = orders.customer_id' in results[0].detail


def test_counterexample_failures_record_nothing(conn):
    led = Ledger()
    metric = _certified_metric(led)
    before = len(metric.evidence)
    with pytest.raises(ValueError, match='nothing was recorded'):
        counterexample_checks(led, conn, metric.id, ['orders.amount < 0', 'orders.nope = 1'])
    with pytest.raises(ValueError, match='non-empty'):
        counterexample_checks(led, conn, metric.id, ['  '])
    with pytest.raises(ValueError, match='no JOIN definition'):
        counterexample_checks(led, conn, metric.id, ['regions.name = 1'])
    with pytest.raises(KeyError):
        counterexample_checks(led, conn, 'no-such-id', ['orders.amount < 0'])
    assert len(metric.evidence) == before and metric.grade == Grade.E3
    assert counterexample_checks(led, conn, metric.id, []) == []


def test_counterexample_uses_generic_sql_for_unknown_drivers(conn):
    led = Ledger()
    metric = _certified_metric(led)
    results = counterexample_checks(led, _Wrapped(conn), metric.id, ['orders.amount < 0'])
    assert results[0].passed and metric.grade == Grade.E4
