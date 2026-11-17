"""Tests for sql_semantic_ledger.parse: statement splitting, alias resolution, normalization and fragments."""
import sqlite3

import pytest
from sqlglot import exp

from sql_semantic_ledger.parse import analyze_query, normalize_expression, parse_query, split_statements


def sqls(fragments):
    return [f.sql for f in fragments]


def by_sql(fragments):
    return {f.sql: f for f in fragments}


def metric_uses(analysis):
    return [(u.aggregate.sql, sqls(u.conditions)) for u in analysis.metric_uses]


# ---------------------------------------------------------------------------------------------- split_statements
def test_split_respects_strings_identifiers_and_comments():
    text = "SELECT 'a;b' FROM t; -- c;\nSELECT \"x;y\" FROM u;;  /* only; a comment */ "
    assert split_statements(text) == ["SELECT 'a;b' FROM t", '-- c;\nSELECT "x;y" FROM u']


def test_split_uses_the_dialect_tokenizer():
    assert split_statements('SELECT $$a;b$$; SELECT 2', dialect='postgres') == ['SELECT $$a;b$$', 'SELECT 2']
    assert split_statements('SELECT 1; # note; more\nSELECT 2', dialect='mysql') == ['SELECT 1', '# note; more\nSELECT 2']
    # text the tokenizer rejects (an unterminated string) still honours the dialect's line comments
    assert split_statements("SELECT 1; # note; more\nSELECT 2; SELECT 'x", dialect='mysql') == \
        ['SELECT 1', '# note; more\nSELECT 2', "SELECT 'x"]


def test_split_empty_text_and_unterminated_string():
    assert split_statements('') == [] and split_statements('  ;; \n') == []
    assert split_statements("SELECT 1; SELECT 'abc") == ['SELECT 1', "SELECT 'abc"]   # tokenizer fails: fallback


def test_split_unknown_dialect_is_an_error():
    with pytest.raises(ValueError):
        split_statements('SELECT 1', dialect='no_such_dialect')


# ---------------------------------------------------------------------------------------------- resolution and normalization
def test_aliases_are_resolved_and_fragments_are_canonical():
    f = parse_query("SELECT SUM(o.amount) FROM orders AS o WHERE 'paid' = o.status", query_id='q7')
    assert f.query_id == 'q7' and f.tables == ['orders']
    assert sqls(f.aggregates) == ['SUM(orders.amount)']
    assert f.aggregates[0].tables == ['orders'] and f.aggregates[0].columns == ['orders.amount']
    assert sqls(f.predicates) == ["orders.status = 'paid'"]
    assert f.predicates[0].columns == ['orders.status']
    assert f.joins == [] and f.group_by == []


def test_unqualified_columns_case_and_quotes_are_normalized():
    a = parse_query("select sum(AMOUNT) from Orders where Status = 'paid'")
    b = parse_query('SELECT SUM("orders"."amount") FROM "orders" WHERE "orders".status = \'paid\'')
    assert sqls(a.aggregates) == sqls(b.aggregates) == ['SUM(orders.amount)']
    assert sqls(a.predicates) == sqls(b.predicates) == ["orders.status = 'paid'"]
    assert a.normalized == b.normalized


def test_normalized_ignores_aliases_format_comments_and_condition_order():
    q1 = parse_query("SELECT SUM(o.amount) FROM orders o WHERE o.status = 'paid' AND o.region = 'EU'")
    q2 = parse_query("select sum(amount)\n  from orders -- monthly report\n where region = 'EU' and status = 'paid'")
    q3 = parse_query("SELECT SUM(x.amount) FROM orders x WHERE x.status = 'paid' AND x.region = 'US'")
    assert q1.normalized == q2.normalized and q1.skeleton == q2.skeleton
    assert q1.normalized != q3.normalized and q1.skeleton == q3.skeleton   # only a literal differs
    assert "'" not in q1.skeleton and '?' in q1.skeleton
    assert q1.normalized.startswith('SELECT\n') and 'monthly' not in q2.normalized


def test_skeleton_collapses_literal_in_lists():
    a = parse_query("SELECT COUNT(*) FROM orders WHERE status IN ('paid', 'shipped')")
    b = parse_query("SELECT COUNT(*) FROM orders WHERE status IN ('refunded')")
    assert a.skeleton == b.skeleton and a.normalized != b.normalized


def test_join_group_by_and_having_with_select_alias():
    f = parse_query("""
        SELECT c.region, SUM(o.amount) AS total
        FROM orders o INNER JOIN customers c ON o.customer_id = c.id
        WHERE c.region = 'EU'
        GROUP BY c.region
        HAVING total > 1000""")
    assert f.tables == ['customers', 'orders']
    assert sqls(f.joins) == ['customers.id = orders.customer_id']
    assert f.joins[0].tables == ['customers', 'orders'] and f.joins[0].columns == ['customers.id', 'orders.customer_id']
    assert f.group_by == ['customers.region']
    assert sqls(f.predicates) == ['SUM(orders.amount) > 1000', "customers.region = 'EU'"]   # HAVING alias expanded
    assert 'JOIN customers' in f.normalized and 'INNER' not in f.normalized


def test_operators_in_between_like_is_null_and_or_groups():
    f = parse_query("""
        SELECT COUNT(1) FROM orders
        WHERE status IN ('shipped', 'paid') AND amount BETWEEN 10 AND 100 AND note LIKE '%gift%'
          AND NOT (email LIKE '%@test.com') AND coupon IS NULL AND email IS NOT NULL
          AND (region = 'US' OR region = 'EU') AND 100 < amount AND status <> 'void' AND 1 = 1""")
    assert sqls(f.aggregates) == ['COUNT(*)']
    assert set(sqls(f.predicates)) == {
        "orders.status IN ('paid', 'shipped')", 'orders.amount BETWEEN 10 AND 100', "orders.note LIKE '%gift%'",
        "orders.email NOT LIKE '%@test.com'", 'orders.coupon IS NULL', 'NOT orders.email IS NULL',
        "orders.region = 'EU' OR orders.region = 'US'", 'orders.amount > 100', "orders.status <> 'void'"}
    assert len(f.predicates) == 9   # the constant condition 1 = 1 is dropped


def test_not_like_keeps_its_negation_when_like_has_no_negate_arg(monkeypatch):
    """sqlglot < 30.8 has no Like 'negate' arg: NOT (x LIKE y) must then stay a NOT, never be dropped."""
    monkeypatch.setattr(exp.Like, 'arg_types', {k: v for k, v in exp.Like.arg_types.items() if k != 'negate'})
    f = parse_query("SELECT COUNT(*) FROM orders WHERE NOT (orders.memo LIKE '%refund%')")
    assert sqls(f.predicates) == ["NOT orders.memo LIKE '%refund%'"]


def test_or_group_with_nested_and_is_one_canonical_predicate():
    a = parse_query('SELECT * FROM t WHERE (b = 2 AND a = 1) OR c = 3')
    b = parse_query('SELECT * FROM t WHERE c = 3 OR (a = 1 AND b = 2)')
    assert sqls(a.predicates) == sqls(b.predicates) == ['(t.a = 1 AND t.b = 2) OR t.c = 3']


def test_cte_columns_are_traced_to_base_tables():
    cte = analyze_query("WITH paid AS (SELECT * FROM orders WHERE status = 'paid') SELECT SUM(p.amount) FROM paid p")
    flat = analyze_query("SELECT SUM(amount) FROM orders WHERE status = 'paid'")
    assert cte.features.tables == ['orders']   # the CTE name is not a table
    assert sqls(cte.features.aggregates) == ['SUM(orders.amount)']
    assert sqls(cte.features.predicates) == ["orders.status = 'paid'"]
    assert metric_uses(cte) == metric_uses(flat) == [('SUM(orders.amount)', ["orders.status = 'paid'"])]
    assert cte.features.skeleton != flat.features.skeleton


def test_cte_column_lists_and_renamed_outputs():
    f = parse_query('WITH t(total_amount) AS (SELECT amount FROM orders) SELECT SUM(total_amount) FROM t')
    g = parse_query('WITH t AS (SELECT o.amount AS amt FROM orders o) SELECT MAX(t.amt) FROM t')
    assert sqls(f.aggregates) == ['SUM(orders.amount)'] and sqls(g.aggregates) == ['MAX(orders.amount)']


def test_grouped_derived_tables_are_not_traced():
    f = parse_query('SELECT AVG(t.total) FROM (SELECT customer_id, SUM(amount) AS total FROM orders '
                    'GROUP BY customer_id) t WHERE t.total > 5')
    assert sqls(f.aggregates) == ['SUM(orders.amount)']   # AVG over a grouped derived table is left out
    assert f.predicates == []                              # and so is the predicate on its computed column
    assert f.group_by == ['orders.customer_id']


def test_subquery_predicates_belong_to_their_own_scope():
    a = analyze_query("SELECT COUNT(*) FROM orders o WHERE o.customer_id IN "
                      "(SELECT id FROM customers WHERE region = 'EU') AND o.status = 'paid'")
    semi = "orders.customer_id IN (SELECT customers.id FROM customers WHERE customers.region = 'EU')"
    predicates = by_sql(a.features.predicates)
    assert set(predicates) == {semi, "customers.region = 'EU'", "orders.status = 'paid'"}
    assert predicates[semi].tables == ['orders'] and 'customers.region' in predicates[semi].columns
    # the subquery's own predicate is not a condition of the outer COUNT(*)
    assert metric_uses(a) == [('COUNT(*)', [semi, "orders.status = 'paid'"])]


def test_subqueries_may_use_their_own_ctes_but_not_outer_ones():
    own = parse_query("SELECT COUNT(*) FROM orders WHERE customer_id IN "
                      "(WITH eu AS (SELECT id FROM customers WHERE region = 'EU') SELECT id FROM eu)")
    assert any(p.sql.startswith('orders.customer_id IN (WITH eu AS') for p in own.predicates)
    outer = parse_query("WITH eu AS (SELECT id FROM customers WHERE region = 'EU') "
                        'SELECT COUNT(*) FROM orders WHERE customer_id IN (SELECT id FROM eu)')
    assert sqls(outer.predicates) == ["customers.region = 'EU'"]   # the IN predicate would need the outer CTE


def test_correlated_subquery_gives_a_join_not_a_predicate():
    f = parse_query("SELECT COUNT(*) FROM customers c WHERE EXISTS "
                    "(SELECT 1 FROM orders o WHERE o.customer_id = c.id AND o.status = 'paid')")
    assert sqls(f.joins) == ['customers.id = orders.customer_id']
    assert "orders.status = 'paid'" in sqls(f.predicates)
    assert 'customers.id = orders.customer_id' not in sqls(f.predicates)
    (exists,) = [p for p in f.predicates if p.sql.startswith('EXISTS')]
    assert exists.tables == ['customers']


def test_correlated_subquery_on_the_same_table_keeps_its_aliases():
    f = parse_query('SELECT COUNT(*) FROM orders o WHERE EXISTS '
                    '(SELECT 1 FROM orders i WHERE i.customer_id = o.customer_id AND i.id <> o.id)')
    assert sqls(f.predicates) == ['EXISTS(SELECT 1 FROM orders AS i WHERE i.customer_id = orders.customer_id '
                                  'AND i.id <> orders.id)']
    assert 'FROM orders AS o' in f.normalized and f.joins == []


def test_self_join_gets_canonical_aliases():
    f = parse_query("SELECT e.name FROM employees e JOIN employees m ON e.manager_id = m.id WHERE m.name = 'Ann'")
    g = parse_query("SELECT a.name FROM employees a JOIN employees b ON a.manager_id = b.id WHERE b.name = 'Ann'")
    assert sqls(f.joins) == ['employees.id = employees.manager_id']
    assert 'employees AS employees_1' in f.normalized and 'employees AS employees_2' in f.normalized
    assert f.normalized == g.normalized


def test_conditions_between_two_instances_of_one_table_are_left_out():
    f = parse_query('SELECT COUNT(*) FROM orders o JOIN customers c ON o.customer_id = c.id '
                    "JOIN customers r ON o.referrer_id = r.id WHERE c.region = r.region AND r.region = 'EU'")
    assert sqls(f.joins) == ['customers.id = orders.customer_id', 'customers.id = orders.referrer_id']
    assert sqls(f.predicates) == ["customers.region = 'EU'"]   # not the role-less "customers.region = customers.region"


def test_implicit_using_inner_and_outer_joins():
    comma = parse_query("SELECT COUNT(*) FROM orders o, customers c WHERE o.customer_id = c.id AND c.region = 'EU'")
    assert sqls(comma.joins) == ['customers.id = orders.customer_id']
    assert sqls(comma.predicates) == ["customers.region = 'EU'"]
    outer = parse_query('SELECT COUNT(*) FROM orders o LEFT JOIN refunds r USING (order_id) '
                        'LEFT JOIN customers c ON o.customer_id = c.id AND c.active = 1 WHERE r.order_id IS NULL')
    assert sqls(outer.joins) == ['customers.id = orders.customer_id', 'orders.order_id = refunds.order_id']
    assert sqls(outer.predicates) == ['refunds.order_id IS NULL']   # a LEFT JOIN ... ON filter is not a row filter
    inner = parse_query('SELECT COUNT(*) FROM orders o JOIN customers c ON o.customer_id = c.id AND c.active = 1')
    assert sqls(inner.predicates) == ['customers.active = 1']        # an INNER JOIN ... ON filter is


def test_left_joined_derived_table_conditions_do_not_apply_to_the_aggregate():
    a = analyze_query("SELECT SUM(o.amount) FROM orders o LEFT JOIN (SELECT * FROM refunds WHERE reason = 'fraud') r "
                      "ON r.order_id = o.id WHERE o.status = 'paid'")
    assert metric_uses(a) == [('SUM(orders.amount)', ["orders.status = 'paid'"])]
    assert sqls(a.features.joins) == ['orders.id = refunds.order_id']
    assert "refunds.reason = 'fraud'" in sqls(a.filters)


def test_count_variants_windows_and_filter_clauses():
    f = parse_query('SELECT COUNT(DISTINCT o.customer_id), COUNT(1), SUM(o.amount) OVER (PARTITION BY o.region), '
                    'MAX(o.amount, 0) FROM orders o')
    assert sqls(f.aggregates) == ['COUNT(*)', 'COUNT(DISTINCT orders.customer_id)', 'SUM(orders.amount)']
    pg = parse_query("SELECT COUNT(*) FILTER (WHERE status = 'paid') FROM orders", dialect='postgres')
    assert sqls(pg.aggregates) == ["COUNT(*) FILTER(WHERE orders.status = 'paid')"]


def test_having_conditions_with_aggregates_are_not_row_filters():
    a = analyze_query("SELECT customer_id, COUNT(*) AS n FROM orders WHERE status = 'paid' "
                      "GROUP BY customer_id HAVING n > 5")
    having = by_sql(a.features.predicates)['COUNT(*) > 5']   # the select alias n is expanded
    assert having.tables == ['orders'] and having.columns == []
    assert sqls(a.filters) == ["orders.status = 'paid'"]
    assert metric_uses(a) == [('COUNT(*)', ["orders.status = 'paid'"])]
    assert a.features.aggregates[0].tables == ['orders']


def test_positional_group_by_and_sqlite_quoting():
    f = parse_query("SELECT strftime('%Y', created_at) AS yr, COUNT(*) FROM orders GROUP BY 1", dialect='sqlite')
    assert f.group_by == ["STRFTIME('%Y', orders.created_at)"]
    g = parse_query('SELECT T2.`Free Meal Count (K-12)` FROM schools AS T1 INNER JOIN frpm AS T2 '
                    "ON T1.CDSCode = T2.CDSCode WHERE T1.County = 'Alameda'", dialect='sqlite')
    assert sqls(g.joins) == ['frpm.cdscode = schools.cdscode']
    assert sqls(g.predicates) == ["schools.county = 'Alameda'"]
    assert 'frpm."free meal count (k-12)"' in g.normalized


def test_union_branches_are_analysed_separately():
    a = analyze_query("SELECT SUM(amount) FROM orders WHERE status = 'paid' "
                      "UNION ALL SELECT SUM(amount) FROM refunds WHERE reason = 'fraud'")
    assert metric_uses(a) == [('SUM(orders.amount)', ["orders.status = 'paid'"]),
                              ('SUM(refunds.amount)', ["refunds.reason = 'fraud'"])]


def test_ambiguous_columns_stay_unqualified_unless_a_schema_decides():
    sql = "SELECT SUM(amount) FROM orders o JOIN customers c ON o.customer_id = c.id WHERE region = 'EU'"
    a = analyze_query(sql)
    assert a.ambiguous and sqls(a.features.predicates) == ["region = 'EU'"]
    assert a.features.predicates[0].tables == ['customers', 'orders']
    b = analyze_query(sql, schema={'customers': ['id', 'region'], 'orders': ['amount', 'customer_id']})
    assert not b.ambiguous
    assert sqls(b.features.predicates) == ["customers.region = 'EU'"]
    assert sqls(b.features.aggregates) == ['SUM(orders.amount)']
    assert a.columns_by_table == {'customers': ['id'], 'orders': ['customer_id']}


def test_parameter_placeholders_are_predicates_but_not_filters():
    a = analyze_query("SELECT SUM(amount) FROM orders WHERE customer_id = ? AND status = 'paid' AND region = :region")
    assert set(sqls(a.features.predicates)) == {'orders.customer_id = ?', "orders.status = 'paid'",
                                                'orders.region = :region'}
    assert sqls(a.filters) == ["orders.status = 'paid'"]
    assert metric_uses(a) == [('SUM(orders.amount)', ["orders.status = 'paid'"])]


@pytest.mark.parametrize('sql', [
    'SELEC * FROM t', '', '   ', 'DROP TABLE t', 'INSERT INTO t SELECT 1', 'UPDATE t SET a = 1',
    'SELECT 1; SELECT 2', 'SELECT * FROM (', 'this is not sql', "SELECT 'unterminated FROM t", None, 42,
    'WITH a AS (SELECT 1) INSERT INTO t SELECT * FROM a'])
def test_unparsable_or_non_select_input_gives_none(sql):
    assert parse_query(sql) is None
    assert analyze_query(sql) is None


def test_unknown_dialect_is_an_error():
    with pytest.raises(ValueError):
        parse_query('SELECT 1', dialect='no_such_dialect')


def test_default_query_id_and_trailing_semicolon():
    f = parse_query('SELECT COUNT(*) FROM orders;  -- done')
    assert f is not None and f.sql == 'SELECT COUNT(*) FROM orders;  -- done'
    assert f.query_id.startswith('q_') and f.query_id == parse_query('select count(*) from orders').query_id


def test_long_or_chains_do_not_hit_recursion_limits():
    f = parse_query('SELECT COUNT(*) FROM t WHERE ' + ' OR '.join(f'a = {i}' for i in range(800)))
    assert f is not None and len(f.predicates) == 1


def test_normalize_expression():
    assert normalize_expression("'paid' = Orders.Status") == "orders.status = 'paid'"
    assert normalize_expression('count(1)') == 'COUNT(*)'
    assert normalize_expression('b.x = a.y') == 'a.y = b.x'
    assert normalize_expression("orders.status IN ('b', 'a') AND orders.amount > 0") == \
        "orders.amount > 0 AND orders.status IN ('a', 'b')"
    with pytest.raises(ValueError):
        normalize_expression('SUM(')
    with pytest.raises(ValueError):
        normalize_expression('SELECT 1')


# ---------------------------------------------------------------------------------------------- meaning is kept
SHOP = """
    CREATE TABLE customers (id INTEGER PRIMARY KEY, region TEXT);
    CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, amount REAL, status TEXT);
    INSERT INTO customers VALUES (1, 'EU'), (2, 'US'), (3, 'EU');
    INSERT INTO orders VALUES (1, 1, 10, 'paid'), (2, 1, 20, 'paid'), (3, 2, 5, 'void'), (4, 3, 7, 'paid'),
                              (5, 2, 50, 'paid');
"""


@pytest.mark.parametrize('sql', [
    "WITH paid AS (SELECT * FROM orders WHERE status = 'paid') SELECT SUM(p.amount) FROM paid p",
    "SELECT COUNT(*) FROM orders o WHERE o.customer_id IN (SELECT id FROM customers WHERE region = 'EU') "
    "AND o.status = 'paid'",
    'SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM orders i WHERE i.customer_id = o.customer_id '
    'AND i.id <> o.id)',
    "SELECT SUM(big.amount) FROM (SELECT * FROM orders WHERE amount > 6) AS big WHERE big.status = 'paid'",
    "SELECT MAX(o.amount) FROM orders o WHERE o.amount < (SELECT AVG(amount) FROM orders) AND 'paid' = o.status",
])
def test_rebuilt_metric_returns_the_query_result(sql):
    conn = sqlite3.connect(':memory:')
    conn.executescript(SHOP)
    (use,) = [u for u in analyze_query(sql, dialect='sqlite').metric_uses if u.conditions]
    (table,) = use.aggregate.tables
    rebuilt = f"SELECT {use.aggregate.sql} FROM {table} WHERE {' AND '.join(c.sql for c in use.conditions)}"
    assert conn.execute(rebuilt).fetchone() == conn.execute(sql).fetchone(), rebuilt


def test_rebuilt_join_metric_returns_the_query_result():
    conn = sqlite3.connect(':memory:')
    conn.executescript(SHOP)
    sql = "SELECT SUM(o.amount) FROM orders o JOIN customers c ON o.customer_id = c.id WHERE c.region = 'EU'"
    a = analyze_query(sql, dialect='sqlite')
    ((aggregate, conditions),) = metric_uses(a)
    (join,) = a.features.joins
    rebuilt = f"SELECT {aggregate} FROM orders JOIN customers ON {join.sql} WHERE {' AND '.join(conditions)}"
    assert conn.execute(rebuilt).fetchone() == conn.execute(sql).fetchone() == (37.0,)
