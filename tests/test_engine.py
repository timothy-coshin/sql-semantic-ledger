"""Tests for engine.assemble_sql / engine.assemble: rule-based SQL assembly from ledger definitions."""
import sqlite3
from types import SimpleNamespace

import pytest
import sqlglot
from sqlglot import exp

from sql_semantic_ledger import Definition, Evidence, EvidenceKind, Kind, Ledger
from sql_semantic_ledger._unimplemented import NotImplementedYet
from sql_semantic_ledger.engine import AssembledQuery, answer, assemble, assemble_sql
from sql_semantic_ledger.revalidate import AnswerRecord, DependencyIndex

SHOP = """
CREATE TABLE customers (id INTEGER PRIMARY KEY, region TEXT, is_test INTEGER DEFAULT 0);
CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, amount REAL, status TEXT);
CREATE TABLE vips (customer_id INTEGER);
INSERT INTO customers VALUES (1, 'EU', 0), (2, 'US', 0), (3, 'EU', 0), (4, 'US', 1);
INSERT INTO orders VALUES (1, 1, 10, 'paid'), (2, 1, 20, 'refunded'), (3, 2, 30, 'paid'), (4, 3, 40, 'paid'),
                          (5, 3, 5, 'shipped'), (6, 4, 100, 'paid'), (7, 2, 7, 'shipped');
INSERT INTO vips VALUES (1), (2);
"""


@pytest.fixture
def db():
    con = sqlite3.connect(':memory:')
    con.executescript(SHOP)
    yield con
    con.close()


def _shop(with_join=True):
    """Two-table ledger: orders and customers, connected by one JOIN definition."""
    led = Ledger()
    s = SimpleNamespace(ledger=led)
    s.revenue = led.add(Definition('revenue', Kind.MEASURE, 'SUM(orders.amount)', tables=['orders']))
    s.paid_revenue = led.add(Definition('paid_revenue', Kind.METRIC, 'SUM(orders.amount)',
                                        conditions=["orders.status = 'paid'"], tables=['orders']))
    s.big = led.add(Definition('big_orders', Kind.FILTER, 'orders.amount >= 30', tables=['orders']))
    s.eu = led.add(Definition('eu_customers', Kind.FILTER, "customers.region = 'EU'", tables=['customers']))
    if with_join:
        s.join = led.add(Definition('join_customers_orders', Kind.JOIN, 'customers.id = orders.customer_id',
                                    tables=['customers', 'orders']))
    return s


def _tree(sql, dialect='sqlite'):
    tree = sqlglot.parse_one(sql, read=dialect)
    assert isinstance(tree, exp.Select)
    return tree


def _where(sql):
    """Top-level AND-ed predicates of the WHERE clause, as SQL text."""
    where = _tree(sql).args.get('where')
    if where is None:
        return []
    cond = where.this
    parts = list(cond.flatten(unnest=False)) if isinstance(cond, exp.And) else [cond]
    return [p.sql(dialect='sqlite') for p in parts]


def _joined(sql, dialect='sqlite'):
    return [j.this.name for j in _tree(sql, dialect).find_all(exp.Join)]


# ---------------------------------------------------------------------- normal cases
def test_measure_alone_reads_only_its_table(db):
    s = _shop()
    sql = assemble_sql(s.ledger, s.revenue.id)
    assert _joined(sql) == [] and [t.name for t in _tree(sql).find_all(exp.Table)] == ['orders']
    assert _where(sql) == []
    assert db.execute(sql).fetchall() == [(212.0,)]


def test_metric_conditions_and_filters_are_anded_into_where(db):
    s = _shop()
    sql = assemble_sql(s.ledger, s.paid_revenue.id, [s.big.id])
    assert _where(sql) == ["orders.status = 'paid'", 'orders.amount >= 30']
    assert db.execute(sql).fetchall() == [(170.0,)]          # paid and >= 30: 30 + 40 + 100


def test_group_by_on_another_table_joins_through_the_join_definition(db):
    s = _shop()
    q = assemble(s.ledger, s.paid_revenue.id, group_by=['customers.region'])
    assert isinstance(q, AssembledQuery)
    assert _joined(q.sql) == ['customers']
    assert 'customers.id = orders.customer_id' in q.sql
    assert q.tables == ['orders', 'customers'] and q.definitions == [s.paid_revenue.id, s.join.id]
    cur = db.execute(q.sql)
    assert [c[0] for c in cur.description] == ['region', 'paid_revenue']
    assert cur.fetchall() == [('EU', 50.0), ('US', 130.0)]   # rows come back ordered by the group columns


def test_filter_on_another_table_is_joined(db):
    s = _shop()
    sql = assemble_sql(s.ledger, s.revenue.id, [s.eu.id])
    assert _joined(sql) == ['customers']
    assert db.execute(sql).fetchall() == [(75.0,)]           # EU orders: 10 + 20 + 40 + 5


def test_several_group_by_columns_keep_their_order_without_repeats(db):
    s = _shop()
    sql = assemble_sql(s.ledger, s.revenue.id, group_by=['customers.region', 'orders.status', 'customers.region'])
    tree = _tree(sql)
    assert [g.sql() for g in tree.args['group'].expressions] == ['customers.region', 'orders.status']
    assert db.execute(sql).fetchall() == [('EU', 'paid', 50.0), ('EU', 'refunded', 20.0), ('EU', 'shipped', 5.0),
                                          ('US', 'paid', 130.0), ('US', 'shipped', 7.0)]


def test_output_is_deterministic():
    s = _shop()
    first = assemble_sql(s.ledger, s.paid_revenue.id, [s.big.id, s.eu.id], ['customers.region'])
    again = assemble_sql(s.ledger, s.paid_revenue.id, [s.eu.id, s.big.id, s.eu.id], ['customers.region'])
    assert first == again
    rebuilt = Ledger()                                       # same definitions, added in another order
    for d in reversed(list(s.ledger)):
        rebuilt.add(Definition.from_dict(d.to_dict()))
    assert assemble_sql(rebuilt, s.paid_revenue.id, [s.big.id, s.eu.id], ['customers.region']) == first


def test_or_filter_keeps_its_precedence(db):
    s = _shop()
    either = s.ledger.add(Definition('paid_or_shipped', Kind.FILTER, "orders.status = 'paid' OR orders.status = 'shipped'",
                                     tables=['orders']))
    sql = assemble_sql(s.ledger, s.revenue.id, [either.id, s.eu.id])
    assert _where(sql) == ["customers.region = 'EU'", "(orders.status = 'paid' OR orders.status = 'shipped')"]
    assert db.execute(sql).fetchall() == [(55.0,)]           # EU and (paid or shipped); without parentheses 62


def test_filter_conditions_are_part_of_the_filter(db):
    s = _shop()
    us = s.ledger.add(Definition('real_us_customers', Kind.FILTER, "customers.region = 'US'",
                                 conditions=['customers.is_test = 0'], tables=['customers']))
    assert db.execute(assemble_sql(s.ledger, s.revenue.id, [us.id])).fetchall() == [(37.0,)]


def test_repeated_predicate_appears_once(db):
    s = _shop()
    paid = s.ledger.add(Definition('paid', Kind.FILTER, "orders.status = 'paid'", tables=['orders']))
    q = assemble(s.ledger, s.paid_revenue.id, [paid.id])
    assert _where(q.sql) == ["orders.status = 'paid'"]
    assert q.definitions == [s.paid_revenue.id, paid.id]
    assert db.execute(q.sql).fetchall() == [(180.0,)]


def test_tables_are_taken_from_the_sql_when_not_declared(db):
    led = Ledger()
    rev = led.add(Definition('revenue', Kind.MEASURE, 'SUM(orders.amount)'))
    eu = led.add(Definition('eu', Kind.FILTER, "customers.region = 'EU'"))
    led.add(Definition('join', Kind.JOIN, 'customers.id = orders.customer_id'))
    assert db.execute(assemble_sql(led, rev.id, [eu.id])).fetchall() == [(75.0,)]


def test_count_star_selects_from_the_declared_table(db):
    s = _shop()
    n = s.ledger.add(Definition('n_orders', Kind.MEASURE, 'COUNT(*)', tables=['orders']))
    sql = assemble_sql(s.ledger, n.id, [s.eu.id])
    assert _tree(sql).find(exp.From).this.name == 'orders'
    assert db.execute(sql).fetchall() == [(4,)]


def test_table_names_match_case_insensitively(db):
    led = Ledger()
    rev = led.add(Definition('revenue', Kind.MEASURE, 'SUM(orders.amount)', tables=['orders']))
    eu = led.add(Definition('eu', Kind.FILTER, "customers.region = 'EU'", tables=['customers']))
    led.add(Definition('join', Kind.JOIN, 'Customers.id = Orders.customer_id', tables=['Customers', 'Orders']))
    sql = assemble_sql(led, rev.id, [eu.id])
    assert _joined(sql) == ['customers']
    assert db.execute(sql).fetchall() == [(75.0,)]


def test_sub_query_tables_are_not_joined(db):
    s = _shop()
    vip = s.ledger.add(Definition('vip_orders', Kind.FILTER,
                                  'orders.customer_id IN (SELECT vips.customer_id FROM vips)', tables=['orders', 'vips']))
    sql = assemble_sql(s.ledger, s.revenue.id, [vip.id])     # no JOIN definition for vips is needed
    assert _joined(sql) == []
    assert db.execute(sql).fetchall() == [(67.0,)]           # customers 1 and 2: 10 + 20 + 30 + 7


def test_correlated_reference_in_a_sub_query_is_joined(db):
    s = _shop()
    vip = s.ledger.add(Definition('vip_customers', Kind.FILTER,
                                  'EXISTS (SELECT 1 FROM vips AS v WHERE v.customer_id = customers.id)', tables=['vips']))
    q = assemble(s.ledger, s.revenue.id, [vip.id])
    assert _joined(q.sql) == ['customers'] and q.tables == ['orders', 'customers']
    assert db.execute(q.sql).fetchall() == [(67.0,)]


def test_aliased_table_in_a_sub_query_does_not_hide_the_outer_reference(db):
    s = _shop()
    shared = s.ledger.add(Definition('shares_region', Kind.FILTER,
                                     'EXISTS (SELECT 1 FROM customers AS c2 WHERE c2.region = customers.region '
                                     "AND c2.id <> customers.id AND c2.is_test = 0)", tables=['customers']))
    sql = assemble_sql(s.ledger, s.revenue.id, [shared.id])
    assert _joined(sql) == ['customers']                     # customers.region is the outer, joined table
    assert db.execute(sql).fetchall() == [(175.0,)]          # only customer 2 lacks a non-test peer: 212 - 37


def test_measure_name_becomes_a_quoted_alias(db):
    led = Ledger()
    keyword = led.add(Definition('order', Kind.MEASURE, 'COUNT(*)', tables=['orders']))
    spaced = led.add(Definition('Paid Revenue', Kind.METRIC, 'SUM(orders.amount)', conditions=["orders.status = 'paid'"],
                                tables=['orders']))
    cur = db.execute(assemble_sql(led, keyword.id))
    assert [c[0] for c in cur.description] == ['order'] and cur.fetchall() == [(7,)]
    cur = db.execute(assemble_sql(led, spaced.id))
    assert [c[0] for c in cur.description] == ['Paid Revenue'] and cur.fetchall() == [(180.0,)]


QUOTED = """
CREATE TABLE "Order Lines" (order_id INTEGER, sku TEXT, qty INTEGER);
CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT);
INSERT INTO orders VALUES (1, 'paid'), (2, 'open');
INSERT INTO "Order Lines" VALUES (1, 'a', 2), (1, 'b', 1), (2, 'a', 4);
"""


@pytest.fixture
def quoted_db():
    con = sqlite3.connect(':memory:')
    con.executescript(QUOTED)
    yield con
    con.close()


def test_quoted_table_names_written_as_sql_text(quoted_db):
    """Definition.tables hold SQL text (``"Order Lines"``), as induce stores them; columns name the same table."""
    led = Ledger()
    qty = led.add(Definition('paid_qty', Kind.METRIC, 'SUM("Order Lines".qty)', conditions=["orders.status = 'paid'"],
                             tables=['"Order Lines"', 'orders']))
    join = led.add(Definition('join_lines_orders', Kind.JOIN, '"Order Lines".order_id = orders.id',
                              tables=['"Order Lines"', 'orders']))
    q = assemble(led, qty.id, group_by=['"Order Lines".sku'])
    assert q.tables == ['"Order Lines"', 'orders'] and q.definitions == [qty.id, join.id]
    assert _joined(q.sql) == ['orders'] and _tree(q.sql).find(exp.From).this.name == 'Order Lines'
    assert quoted_db.execute(q.sql).fetchall() == [('a', 2), ('b', 1)]


def test_quoted_names_match_case_insensitively_and_raw_names_are_quoted(quoted_db):
    led = Ledger()
    lower = led.add(Definition('qty', Kind.MEASURE, 'SUM("order lines".qty)', tables=['"Order Lines"']))
    raw = led.add(Definition('raw_qty', Kind.MEASURE, 'SUM("Order Lines".qty)', tables=['Order Lines']))  # not SQL
    for d in (lower, raw):
        q = assemble(led, d.id)
        assert len(q.tables) == 1 and _joined(q.sql) == []
        assert quoted_db.execute(q.sql).fetchall() == [(7,)]
    assert assemble(led, raw.id).tables == ['"Order Lines"']


def test_other_dialects():
    s = _shop()
    pg = assemble_sql(s.ledger, s.paid_revenue.id, [s.eu.id], ['customers.region'], dialect='postgres')
    assert _joined(pg, 'postgres') == ['customers'] and '"paid_revenue"' in pg
    my = assemble_sql(s.ledger, s.paid_revenue.id, [s.eu.id], ['customers.region'], dialect='mysql')
    assert _joined(my, 'mysql') == ['customers'] and '`paid_revenue`' in my


# ---------------------------------------------------------------------- joins: shortest path, choice of definition
GEO = """
CREATE TABLE regions (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE customers (id INTEGER PRIMARY KEY, region_id INTEGER);
CREATE TABLE stores (id INTEGER PRIMARY KEY, city_id INTEGER);
CREATE TABLE cities (id INTEGER PRIMARY KEY, region_id INTEGER);
CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, store_id INTEGER, amount REAL);
INSERT INTO regions VALUES (1, 'North'), (2, 'South');
INSERT INTO customers VALUES (1, 1), (2, 2);
INSERT INTO stores VALUES (1, 1);
INSERT INTO cities VALUES (1, 2);
INSERT INTO orders VALUES (1, 1, 1, 10), (2, 2, 1, 20);
"""


def _geo(short_path=True):
    led = Ledger()
    rev = led.add(Definition('revenue', Kind.MEASURE, 'SUM(orders.amount)', tables=['orders']))
    joins = {'orders-customers': 'customers.id = orders.customer_id', 'orders-stores': 'stores.id = orders.store_id',
             'stores-cities': 'cities.id = stores.city_id', 'cities-regions': 'regions.id = cities.region_id'}
    if short_path:
        joins['customers-regions'] = 'regions.id = customers.region_id'
    ids = {name: led.add(Definition(name, Kind.JOIN, sql)).id for name, sql in joins.items()}
    return led, rev, ids


def test_shortest_join_path_is_used():
    con = sqlite3.connect(':memory:')
    con.executescript(GEO)
    led, rev, ids = _geo()
    q = assemble(led, rev.id, group_by=['regions.name'])
    assert _joined(q.sql) == ['customers', 'regions']        # 2 hops, not orders-stores-cities-regions
    assert q.definitions == [rev.id, ids['orders-customers'], ids['customers-regions']]
    assert con.execute(q.sql).fetchall() == [('North', 10.0), ('South', 20.0)]
    led, rev, ids = _geo(short_path=False)                    # only the long way is left
    q = assemble(led, rev.id, group_by=['regions.name'])
    assert _joined(q.sql) == ['stores', 'cities', 'regions']
    assert con.execute(q.sql).fetchall() == [('South', 30.0)]
    con.close()


def test_best_join_definition_between_two_tables_is_used():
    led = Ledger()
    rev = led.add(Definition('revenue', Kind.MEASURE, 'SUM(orders.amount)', tables=['orders']))
    eu = led.add(Definition('eu', Kind.FILTER, "customers.region = 'EU'", tables=['customers']))
    buyer = led.add(Definition('join_buyer', Kind.JOIN, 'customers.id = orders.customer_id'))
    billing = led.add(Definition('join_billing', Kind.JOIN, 'customers.id = orders.billing_id'))
    for ref in ('q1', 'q2'):
        led.add_evidence(billing.id, Evidence(EvidenceKind.HISTORY, ref=ref))      # E1
    led.certify(buyer.id, by='analyst')                                              # E3
    q = assemble(led, rev.id, [eu.id])
    assert q.definitions[-1] == buyer.id and 'orders.customer_id' in q.sql
    led.add_evidence(buyer.id, Evidence(EvidenceKind.DATA_CHECK, ref='joins_rows', passed=False))  # falsified: E0
    q = assemble(led, rev.id, [eu.id])
    assert q.definitions[-1] == billing.id and 'orders.billing_id' in q.sql


def test_assembled_query_feeds_the_revalidation_index():
    s = _shop()
    q = assemble(s.ledger, s.paid_revenue.id, [s.eu.id], ['customers.region'])
    assert q.definitions == [s.paid_revenue.id, s.eu.id, s.join.id] and q.tables == ['orders', 'customers']
    assert q.sql == assemble_sql(s.ledger, s.paid_revenue.id, [s.eu.id], ['customers.region'])
    index = DependencyIndex([AnswerRecord('a1', q.definitions, q.tables)])
    assert index.affected_by_definitions([s.join.id]) == {'a1'} and index.affected_by_tables(['CUSTOMERS']) == {'a1'}


# ---------------------------------------------------------------------- failure cases
def test_unconnected_table_raises_value_error():
    s = _shop(with_join=False)
    with pytest.raises(ValueError, match=r"'customers' \(needed by filter 'eu_customers'\) is not connected to orders"):
        assemble_sql(s.ledger, s.revenue.id, [s.eu.id])
    with pytest.raises(ValueError, match='not connected'):
        assemble_sql(s.ledger, s.revenue.id, group_by=['customers.region'])
    s.ledger.add(Definition('join_orders_vips', Kind.JOIN, 'vips.customer_id = orders.customer_id'))
    broken = s.ledger.add(Definition('broken', Kind.JOIN, 'customers.id =', tables=['customers', 'orders']))
    with pytest.raises(ValueError, match=f'ignored as edges: {broken.id}'):
        assemble_sql(s.ledger, s.revenue.id, [s.eu.id])


def test_join_definition_over_three_tables_is_not_an_edge():
    s = _shop(with_join=False)
    s.ledger.add(Definition('odd', Kind.JOIN, 'customers.id = orders.customer_id AND vips.customer_id = customers.id'))
    with pytest.raises(ValueError, match='not connected'):
        assemble_sql(s.ledger, s.revenue.id, [s.eu.id])


def test_wrong_ids_and_kinds():
    s = _shop()
    with pytest.raises(KeyError):
        assemble_sql(s.ledger, 'no-such-id')
    with pytest.raises(KeyError):
        assemble_sql(s.ledger, s.revenue.id, ['no-such-id'])
    with pytest.raises(ValueError, match='measure or metric'):
        assemble_sql(s.ledger, s.eu.id)
    with pytest.raises(ValueError, match='filter definitions'):
        assemble_sql(s.ledger, s.revenue.id, [s.paid_revenue.id])
    with pytest.raises(ValueError, match='filter definitions'):
        assemble_sql(s.ledger, s.revenue.id, [s.join.id])
    with pytest.raises(TypeError):
        assemble_sql(s.ledger, s.revenue.id, s.eu.id)              # a bare string, not a list of ids
    with pytest.raises(TypeError):
        assemble_sql(s.ledger, s.revenue.id, group_by='customers.region')
    with pytest.raises(TypeError):
        assemble_sql({}, s.revenue.id)


@pytest.mark.parametrize('item', ['region', 'customers.*', 'customers.region + 1', '', 'customers.region; x'])
def test_group_by_must_be_qualified_column_names(item):
    s = _shop()
    with pytest.raises(ValueError, match='qualified column name'):
        assemble_sql(s.ledger, s.revenue.id, group_by=[item])


def test_unknown_dialect():
    s = _shop()
    with pytest.raises(ValueError, match='dialect'):
        assemble_sql(s.ledger, s.revenue.id, dialect='no-such-dialect')


@pytest.mark.parametrize('expression, message', [
    ('orders.amount >', 'does not parse'),
    ('orders.amount > 1; DROP TABLE orders', 'one SQL expression'),
    ('SELECT 1', 'not a SQL expression'),
    ('SUM(orders.amount) > 100', 'HAVING'),
    ('   ', 'empty'),
])
def test_bad_filter_fragments_raise_value_error(expression, message):
    s = _shop()
    bad = s.ledger.add(Definition('bad', Kind.FILTER, expression, tables=['orders']))
    with pytest.raises(ValueError, match=message):
        assemble_sql(s.ledger, s.revenue.id, [bad.id])


def test_measure_without_any_table():
    led = Ledger()
    n = led.add(Definition('n', Kind.MEASURE, 'COUNT(*)'))
    with pytest.raises(ValueError, match='names no table'):
        assemble_sql(led, n.id)


def test_natural_language_answer_is_planned():
    with pytest.raises(NotImplementedYet, match='trust.engine_nl'):
        answer('What was the paid revenue last quarter?', Ledger())
