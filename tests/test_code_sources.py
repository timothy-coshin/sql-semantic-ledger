"""Tests for code_sources: SQL queries found in JavaScript, TypeScript and Python service code."""
from __future__ import annotations

from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

from sql_semantic_ledger import EvidenceKind
from sql_semantic_ledger._unimplemented import NotImplementedYet
from sql_semantic_ledger.code_sources import (DEFAULT_EXCLUDE, DEFAULT_INCLUDE, PLACEHOLDER, extract_sql,
                                              lift_js_computation, orm_queries, scan_repository)
from sql_semantic_ledger.induce import QueryRecord

# ------------------------------------------------------------------------------------------- fixtures
JS_ORDERS = """\
// Orders API. SQL in a comment is ignored: SELECT * FROM audit_log
const express = require('express');
const router = express.Router();
const HELP = "Select a file to upload";       // UI text, not SQL
const QUOTES = /["']/g;                         // a regex literal with quotes in it
/* block comment with 'SELECT id FROM hidden' */

router.get('/revenue', async (req, res) => {
  const rows = await db.query(`
    SELECT SUM(o.amount) AS revenue
    FROM orders o
    WHERE o.status = ${req.query.status} AND o.region = '${region}'
  `);
  res.json(rows.map((r) => r.revenue / 100));
});
module.exports = router;
"""

TS_CUSTOMERS = """\
import { Pool } from 'pg';

export interface Customer { id: number; region: string }
const pool = new Pool();
const SIGNATURE: string = 'With best regards';  // starts with WITH but is not SQL

export async function activeCustomers(region: string): Promise<number> {
  const sql = 'SELECT COUNT(DISTINCT c.id) AS n FROM customers c ' +
              "WHERE c.region = $1 AND c.status = 'active'";
  const { rows } = await pool.query<{ n: number }>(sql, [region]);
  return rows[0].n;
}

export const RECENT = `WITH recent AS (SELECT * FROM orders WHERE created_at >= ?)
SELECT COUNT(*) AS n FROM recent`;
"""

PY_REPORTS = '''\
"""Select helpers for the reports service; this docstring is not SQL."""
import sqlite3

PAID_BY_CUSTOMER = """
    SELECT customer_id, SUM(amount) AS total
    FROM orders
    WHERE status = 'paid'
    GROUP BY customer_id
"""


def by_region(cur, region, table, cid):
    """SELECT id FROM docstrings_are_skipped"""
    # cur.execute("SELECT * FROM commented_out")
    cur.execute("SELECT * FROM customers WHERE region = %s", (region,))
    cur.execute(f"SELECT COUNT(*) FROM {table}")  # the table itself is dynamic: skipped
    cur.execute(f"SELECT COUNT(*) FROM orders WHERE customer_id = {cid} AND note LIKE '%{{x}}%'")
    query = (
        "SELECT id, amount "
        "FROM orders "  # a comment between the parts
        "WHERE status = %(status)s"
    )
    return query
'''


def line_of(text: str, needle: str) -> int:
    """1-based line of the first occurrence of ``needle`` (computed independently of the module)."""
    return text.count('\n', 0, text.index(needle)) + 1


def tables_of(sql: str) -> list:
    return sorted(t.name for t in sqlglot.parse_one(sql).find_all(exp.Table))


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    files = {
        'api/orders.js': JS_ORDERS,
        'src/customers.ts': TS_CUSTOMERS,
        'reports.py': PY_REPORTS,
        'node_modules/pkg/index.js': 'module.exports = () => db.query("SELECT * FROM vendored_table");\n',
        'web/node_modules/x/y.ts': "export const Q = 'SELECT * FROM nested_vendored';\n",
        'dist/bundle.js': 'var q="SELECT * FROM bundled_table";\n',
        'docs/notes.md': 'Run `SELECT name FROM docs_table` to check.\n',
    }
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    (tmp_path / 'binary.js').write_bytes(b'\x00\x01 const q = "SELECT * FROM binary_table";')
    return tmp_path


# ------------------------------------------------------------------------------------------- per language
def test_javascript_template_literal_and_non_sql_strings():
    records = extract_sql(JS_ORDERS, path='api/orders.js')
    assert len(records) == 1, [r.sql for r in records]
    rec = records[0]
    assert rec.ref == 'api/orders.js:9' == f'api/orders.js:{line_of(JS_ORDERS, "db.query(`")}'
    assert rec.kind == EvidenceKind.CODE
    assert 'o.status = ?' in rec.sql and 'o.region = ?' in rec.sql and "'?'" not in rec.sql and '${' not in rec.sql
    assert rec.sql.startswith('SELECT SUM(o.amount)') and rec.sql.endswith('o.region = ?')
    assert tables_of(rec.sql) == ['orders']


def test_typescript_concatenation_and_with_query():
    records = extract_sql(TS_CUSTOMERS, path='src/customers.ts')
    assert [r.ref for r in records] == ['src/customers.ts:8', 'src/customers.ts:14']
    assert records[0].sql == ("SELECT COUNT(DISTINCT c.id) AS n FROM customers c "
                              "WHERE c.region = $1 AND c.status = 'active'")
    assert records[1].sql.startswith('WITH recent AS (') and records[1].sql.endswith('FROM recent')
    assert all(r.kind == EvidenceKind.CODE for r in records)


def test_python_triple_quoted_fstring_percent_and_implicit_concatenation():
    records = extract_sql(PY_REPORTS, path='reports.py')
    assert [r.ref for r in records] == ['reports.py:4', 'reports.py:15', 'reports.py:17', 'reports.py:19']
    paid, customers, fstring, joined = (r.sql for r in records)
    assert paid.startswith('SELECT customer_id, SUM(amount) AS total') and paid.endswith('GROUP BY customer_id')
    assert customers == 'SELECT * FROM customers WHERE region = ?'
    assert fstring == "SELECT COUNT(*) FROM orders WHERE customer_id = ? AND note LIKE '%{x}%'"
    assert joined == 'SELECT id, amount FROM orders WHERE status = ?'
    assert not any('docstrings_are_skipped' in s or 'commented_out' in s for s in (paid, customers, fstring, joined))


def test_record_fields_and_placeholder():
    rec, = extract_sql("const q = 'SELECT a FROM t WHERE b = ${x}';\n", path='a.js')
    assert isinstance(rec, QueryRecord)
    assert (rec.sql, rec.id, rec.ref, rec.kind, rec.user, rec.at) == (
        'SELECT a FROM t WHERE b = ?', 'a.js:1:11', 'a.js:1', EvidenceKind.CODE, None, None)
    assert PLACEHOLDER == '?'


# ------------------------------------------------------------------------------------------- JavaScript lexer
def test_regex_literal_with_quote_does_not_hide_sql():
    src = "const QUOTE = /'/g, q = 'SELECT a FROM t WHERE b = 1';\nconst half = total / 2, r = 'SELECT c FROM u';\n"
    assert [(r.ref, r.sql) for r in extract_sql(src, path='x.js')] == [
        ('x.js:1', 'SELECT a FROM t WHERE b = 1'), ('x.js:2', 'SELECT c FROM u')]


def test_jsx_apostrophe_and_unterminated_string_resynchronise():
    src = "const view = <p>Don't panic</p>;\nconst broken = 'not closed\nconst q = \"SELECT id FROM orders\";\n"
    assert [(r.ref, r.sql) for r in extract_sql(src, path='v.jsx')] == [('v.jsx:3', 'SELECT id FROM orders')]


def test_nested_template_escaped_interpolation_and_quote_escapes():
    src = r"""const q1 = `SELECT * FROM orders WHERE status = ${paid ? `'paid'` : "'open'"} AND amount > 0`;
const q2 = `SELECT a FROM t WHERE b = '\${literal}'`;
const q3 = 'SELECT * FROM t WHERE name = \'x\' ' +
  // the second part
  "AND tag = \"y\"";
"""
    assert [r.sql for r in extract_sql(src, path='q.js')] == [
        'SELECT * FROM orders WHERE status = ? AND amount > 0',
        "SELECT a FROM t WHERE b = '${literal}'",
        "SELECT * FROM t WHERE name = 'x' AND tag = \"y\"",
    ]


def test_plain_js_string_dollar_fields_and_comments_are_not_literals():
    src = "// const a = 'SELECT a FROM commented';\n/* 'SELECT b FROM block' */\nconst c = 'SELECT c FROM t WHERE id = ${id}';\n"
    assert [(r.ref, r.sql) for r in extract_sql(src, path='c.js')] == [('c.js:3', 'SELECT c FROM t WHERE id = ?')]


# ------------------------------------------------------------------------------------------- Python lexer
def test_python_parameter_styles_outside_sql_quotes_only():
    src = r'''a = "SELECT * FROM t WHERE id = {} AND region = {region!r}"
b = "SELECT * FROM t WHERE doc = '{\"k\": 1}' AND id = %s"
c = "SELECT a %% 2 AS parity FROM t WHERE b = %(b)s AND c = ?1"
d = "SELECT * FROM t WHERE name LIKE '%son%' AND id = %s"
e = "SELECT a -- don't remove\nFROM t WHERE id = %s"
f = f"SELECT * FROM events WHERE day = '{day:%Y-%m-%d}' AND k = {row["k"]}"
g = "SELECT * FROM t WHERE name = 'O''Brien %s' AND id = %s"
'''
    assert [r.sql for r in extract_sql(src, path='p.py')] == [
        'SELECT * FROM t WHERE id = ? AND region = ?',
        "SELECT * FROM t WHERE doc = '{\"k\": 1}' AND id = ?",
        'SELECT a % 2 AS parity FROM t WHERE b = ? AND c = ?',
        "SELECT * FROM t WHERE name LIKE '%son%' AND id = ?",
        "SELECT a -- don't remove\nFROM t WHERE id = ?",
        'SELECT * FROM events WHERE day = ? AND k = ?',
        "SELECT * FROM t WHERE name = 'O''Brien %s' AND id = ?",
    ]


def test_python_prefixes_and_concatenation_forms():
    src = r'''r1 = r"SELECT a FROM t WHERE b LIKE 'x\_y'"
b1 = b'SELECT a FROM t'
q = ("SELECT a "   # columns
     'FROM t ' f"WHERE id = {i}")
plus = "SELECT x " + \
    "FROM u"
'''
    records = extract_sql(src, path='p.py')
    assert [(r.ref, r.sql) for r in records] == [
        ('p.py:1', r"SELECT a FROM t WHERE b LIKE 'x\_y'"),
        ('p.py:2', 'SELECT a FROM t'),
        ('p.py:3', 'SELECT a FROM t WHERE id = ?'),
        ('p.py:5', 'SELECT x FROM u'),
    ]


def test_python_statements_are_not_joined_and_docstrings_are_skipped():
    src = 'def f():\n    """SELECT id FROM docstring_table"""\n    return 1\n\na = "SELECT a FROM t"\n"SELECT b FROM bare_statement"\n'
    assert [(r.ref, r.sql) for r in extract_sql(src, path='m.py')] == [('m.py:5', 'SELECT a FROM t')]


def test_python_dynamic_table_and_unterminated_string_are_skipped():
    src = 'q = f"SELECT * FROM {table} WHERE id = 1"\nbroken = "not closed\nok = \'SELECT a FROM t\'\n'
    assert [(r.ref, r.sql) for r in extract_sql(src, path='d.py')] == [('d.py:3', 'SELECT a FROM t')]


def test_python_escapes_are_decoded():
    src = "q = 'SELECT a\\tFROM t WHERE b = \\'x\\' AND c = \\x27y\\x27'\n"
    rec, = extract_sql(src, path='e.py')
    assert rec.sql == "SELECT a\tFROM t WHERE b = 'x' AND c = 'y'"


# ------------------------------------------------------------------------------------------- validation rules
def test_only_queries_that_read_a_named_table_are_kept():
    src = r"""const a = 'Selection of orders';             // SELECT must be a whole word
const b = 'SELECT 1';                          // reads no table
const c = 'Select a file';                     // UI text that sqlglot reads as SELECT a AS file
const d = 'WITH c AS (SELECT 1) INSERT INTO t SELECT * FROM c';   // not a query
const e = 'select id from orders';             // lower case is fine
const f = '  \n  SELECT id FROM orders';       // leading whitespace
const g = 'SELECT (a FROM t';                  // does not parse
const h = `${prefix} SELECT id FROM orders`;   // dynamic start
"""
    assert [(r.ref, r.sql) for r in extract_sql(src, path='v.js')] == [
        ('v.js:5', 'select id from orders'), ('v.js:6', 'SELECT id FROM orders')]


def test_interpolation_inside_sql_quotes_is_one_placeholder():
    src = """const a = `SELECT COUNT(*) FROM users WHERE name LIKE '%${q}%' AND note = 'it''s ${x}'`;
const b = `SELECT COUNT(*) FROM "${table}" WHERE id = 1`;    // a quoted dynamic table is still dynamic
const c = `SELECT COUNT(*) FROM users WHERE name = 'it''s' AND tag = "a${t}b"`;
"""
    assert [r.sql for r in extract_sql(src, path='q.js')] == [
        'SELECT COUNT(*) FROM users WHERE name LIKE ? AND note = ?',
        "SELECT COUNT(*) FROM users WHERE name = 'it''s' AND tag = ?"]
    py = "q = f\"SELECT SUM(amount) FROM orders WHERE region = '{region}' AND status = 'paid'\"\n"
    assert [r.sql for r in extract_sql(py, path='q.py')] == [
        "SELECT SUM(amount) FROM orders WHERE region = ? AND status = 'paid'"]


def test_a_value_interpolated_in_quotes_does_not_become_a_constant_filter():
    from sql_semantic_ledger.induce import induce

    src = """const a = `SELECT SUM(o.amount) FROM orders o WHERE o.region = '${r}' AND o.status = 'paid'`;
const b = `SELECT COUNT(*) FROM orders WHERE region = '${region}' AND status = 'paid'`;
"""
    ledger = induce(extract_sql(src, path='api.js'))
    expressions = sorted(d.expression for d in ledger)
    assert expressions == ["orders.status = 'paid'"]           # no "orders.region = '?'" filter
    assert "'?'" not in ' '.join(e.detail for d in ledger for e in d.evidence)


def test_dynamic_structure_is_skipped_but_dynamic_values_are_kept():
    src = """const a = `SELECT SUM(amount) FROM orders ${where}`;        // an interpolated clause
const b = `SELECT * FROM t AS ${alias} WHERE t.id = 1`;      // a dynamic alias
const c = `SELECT t.${column} FROM t`;                       // a dynamic column name
const d = `SELECT * FROM ${schema}.orders WHERE id = ${id}`; // a dynamic schema is fine
const e = `SELECT ${cols} FROM t WHERE a = ${a} ORDER BY ${sort} LIMIT ${n}`;
"""
    assert [(r.ref, r.sql) for r in extract_sql(src, path='dyn.js')] == [
        ('dyn.js:4', 'SELECT * FROM ?.orders WHERE id = ?'),
        ('dyn.js:5', 'SELECT ? FROM t WHERE a = ? ORDER BY ? LIMIT ?')]


def test_unicode_escapes_are_decoded():
    bs = '\\'
    js = (f"const q = 'SELECT a FROM t WHERE n = {bs}'{bs}u00e9{bs}' AND m = {bs}'{bs}u{{1F600}}{bs}' "
          f"AND p = {bs}'{bs}uD83D{bs}uDE00{bs}'';\n")
    rec, = extract_sql(js, path='u.js')
    assert rec.sql == f"SELECT a FROM t WHERE n = '{chr(0xE9)}' AND m = '{chr(0x1F600)}' AND p = '{chr(0x1F600)}'"
    py = f"q = 'SELECT a FROM t WHERE n = {bs}'{bs}N{{LATIN SMALL LETTER E WITH ACUTE}}{bs}' AND m = {bs}'{bs}u00e9{bs}''\n"
    rec, = extract_sql(py, path='u.py')
    assert rec.sql == f"SELECT a FROM t WHERE n = '{chr(0xE9)}' AND m = '{chr(0xE9)}'"


def test_several_statements_in_one_literal():
    records = extract_sql('QS = "SELECT a FROM t; SELECT b FROM u; DELETE FROM t"\n', path='x.py')
    assert [(r.id, r.ref, r.sql) for r in records] == [
        ('x.py:1:6.1', 'x.py:1', 'SELECT a FROM t'), ('x.py:1:6.2', 'x.py:1', 'SELECT b FROM u')]


def test_dialect_is_used_for_the_parse_check():
    src = "const q = \"SELECT a FROM t WHERE b ~ 'x'\";\n"
    assert extract_sql(src, path='r.js') == []
    assert [r.sql for r in extract_sql(src, path='r.js', dialect='postgres')] == ["SELECT a FROM t WHERE b ~ 'x'"]


def test_crlf_line_endings_and_byte_order_mark():
    src = chr(0xFEFF) + 'const a = 1;\r\nconst q = "SELECT a FROM t";\r\n'
    rec, = extract_sql(src)
    assert rec.ref == '<string>:2' and rec.sql == 'SELECT a FROM t'


def test_language_inference_and_aliases():
    py = 'q = """SELECT a\nFROM t"""\n'
    assert [r.ref for r in extract_sql(py, language='py')] == ['<string>:1']
    assert [r.ref for r in extract_sql(py, path='jobs/q.py')] == ['jobs/q.py:1']
    # without a language hint, '<string>' is lexed as JavaScript, where a newline ends a "..." string
    assert extract_sql(py) == []


def test_bad_arguments_raise_clear_errors():
    with pytest.raises(ValueError, match='unknown language'):
        extract_sql("q = 'SELECT a FROM t'", language='cobol')
    with pytest.raises(ValueError, match='nope'):
        extract_sql("q = 'SELECT a FROM t'", dialect='nope')
    with pytest.raises(TypeError):
        extract_sql(b"q = 'SELECT a FROM t'")


# ------------------------------------------------------------------------------------------- repository scan
def test_scan_repository_excludes_vendored_and_binary_files(repo: Path):
    records = scan_repository(repo)
    assert [r.ref for r in records] == [
        'api/orders.js:9', 'reports.py:4', 'reports.py:15', 'reports.py:17', 'reports.py:19',
        'src/customers.ts:8', 'src/customers.ts:14']
    assert all(r.kind == EvidenceKind.CODE for r in records)
    assert len({r.id for r in records}) == len(records)
    joined = ' '.join(r.sql for r in records)
    for hidden in ('vendored_table', 'nested_vendored', 'bundled_table', 'docs_table', 'binary_table'):
        assert hidden not in joined
    for r in records:
        assert sqlglot.parse_one(r.sql) is not None


def test_scan_repository_is_deterministic(repo: Path):
    assert scan_repository(repo) == scan_repository(str(repo))


def test_scan_repository_include_exclude_patterns(repo: Path):
    assert {r.ref.split(':')[0] for r in scan_repository(repo, include='**/*.py')} == {'reports.py'}
    assert {r.ref.split(':')[0] for r in scan_repository(repo, exclude=list(DEFAULT_EXCLUDE) + ['reports.py'])} == {
        'api/orders.js', 'src/customers.ts'}
    docs = scan_repository(repo, include=['docs/*.md'], exclude=())
    assert [(r.ref, r.sql) for r in docs] == [('docs/notes.md:1', 'SELECT name FROM docs_table')]
    vendored = scan_repository(repo, include=['**/node_modules/**'])
    assert vendored == []          # the default excludes still prune node_modules


def test_scan_single_file_and_missing_root(repo: Path):
    assert [r.ref for r in scan_repository(repo / 'reports.py')] == [
        'reports.py:4', 'reports.py:15', 'reports.py:17', 'reports.py:19']
    with pytest.raises(FileNotFoundError):
        scan_repository(repo / 'does_not_exist')
    with pytest.raises(ValueError):
        scan_repository(repo, dialect='nope')


def test_default_patterns_and_planned_features():
    assert '**/*.py' in DEFAULT_INCLUDE and '**/*.ts' in DEFAULT_INCLUDE
    assert '**/node_modules/**' in DEFAULT_EXCLUDE
    with pytest.raises(NotImplementedYet):
        orm_queries()
    with pytest.raises(NotImplementedYet):
        lift_js_computation()
