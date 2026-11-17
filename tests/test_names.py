"""Tests for the internal helpers that read and write table names given as SQL text (_names)."""
import pytest
import sqlglot
from sqlglot import exp

from sql_semantic_ledger import _names


@pytest.mark.parametrize('text, parts', [
    ('orders', (('orders', False),)),
    ('  orders ', (('orders', False),)),
    ('main.orders', (('main', False), ('orders', False))),
    ('"Order Lines"', (('Order Lines', True),)),
    ('main."Order Lines"', (('main', False), ('Order Lines', True))),
    ('cat.db.tbl', (('cat', False), ('db', False), ('tbl', False))),
    ('"a""b"', (('a"b', True),)),
    ('"my.table"', (('my.table', True),)),
    ('order lines', (('order lines', True),)),           # not SQL: one exact name
    ('orders o', (('orders o', True),)),                 # an alias is not part of a table name
    ('orders(x)', (('orders(x)', True),)),               # nor is a function call
    ('', ()),
])
def test_table_parts(text, parts):
    assert _names.table_parts(text, 'sqlite') == parts


def test_table_parts_follow_the_dialect_quotes():
    assert _names.table_parts('`order lines`', 'mysql') == (('order lines', True),)
    assert _names.table_parts('[order lines]', 'tsql') == (('order lines', True),)
    assert _names.table_parts('`order lines`', 'postgres') == (('`order lines`', True),)   # not SQL in PostgreSQL


@pytest.mark.parametrize('text, key, bare', [
    ('Orders', 'orders', 'orders'),
    ('"Order Lines"', 'order lines', 'order lines'),
    ('Order Lines', 'order lines', 'order lines'),
    ('MAIN."Orders"', 'main.orders', 'orders'),
])
def test_table_key(text, key, bare):
    assert _names.table_key(text, 'sqlite') == key
    assert _names.table_key(text, 'sqlite', schema=False) == bare


@pytest.mark.parametrize('text, sql', [
    ('orders', 'orders'),
    ('order lines', '"order lines"'),
    ('"Order Lines"', '"Order Lines"'),
    ('main."Order Lines"', 'main."Order Lines"'),
    ('"group"', '"group"'),
    ('group', '"group"'),                                # a keyword is quoted
    ('user', '"user"'),
    ('Orders', 'Orders'),                                # case is kept as written
])
def test_table_sql(text, sql):
    assert _names.table_sql(text, 'sqlite') == sql
    assert sqlglot.parse_one(sql, into=exp.Table, read='sqlite') is not None


def test_table_sql_in_other_dialects_and_long_names():
    assert _names.table_sql('order lines', 'mysql') == '`order lines`'
    assert _names.table_sql('order lines', 'tsql') == '[order lines]'
    assert _names.table_sql('srv.db.sch.tbl', 'tsql') == 'srv.db.sch.tbl'
    with pytest.raises(ValueError):
        _names.parts_table((), 'sqlite')


def test_identifier_exact_case_quotes_names_the_dialect_would_fold():
    assert _names.identifier('Orders', 'postgres').sql('postgres') == 'Orders'
    assert _names.identifier('Orders', 'postgres', exact_case=True).sql('postgres') == '"Orders"'
    assert _names.identifier('orders', 'postgres', exact_case=True).sql('postgres') == 'orders'
    assert _names.identifier('ORDERS', 'snowflake', exact_case=True).sql('snowflake') == 'ORDERS'
    assert _names.identifier('orders', 'snowflake', exact_case=True).sql('snowflake') == '"orders"'
    assert _names.identifier('Orders', 'sqlite', exact_case=True).sql('sqlite') == 'Orders'   # SQLite ignores case


def test_fold_like_the_database():
    assert _names.folds_case('postgres') and not _names.folds_case('sqlite')
    assert _names.fold_name('Orders', False, 'postgres') == 'orders'
    assert _names.fold_name('Orders', True, 'postgres') == 'Orders'
    assert _names.fold_name('Orders', False, 'sqlite') == 'Orders'
    tree = _names.fold(sqlglot.parse_one('"Order Lines".Qty + Orders.Amount', read='postgres'), 'postgres')
    assert tree.sql('postgres') == '"Order Lines".qty + orders.amount'
    same = sqlglot.parse_one('"Order Lines".Qty', read='sqlite')
    assert _names.fold(same, 'sqlite').sql('sqlite') == '"Order Lines".Qty'


def test_column_table_parts():
    column = sqlglot.parse_one('main."Order Lines".qty', read='sqlite')
    assert _names.column_table_parts(column) == (('main', False), ('Order Lines', True))
    assert _names.column_table_parts(sqlglot.parse_one('qty')) == ()
