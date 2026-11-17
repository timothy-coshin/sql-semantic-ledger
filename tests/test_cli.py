"""Tests for the ``ledger`` command line (cli.main) on the toy shop example, and for the example's demo.py."""
import importlib.util
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from sql_semantic_ledger import Grade, Kind, Ledger
from sql_semantic_ledger.cli import main
from sql_semantic_ledger.features import table
from sql_semantic_ledger.induce import induce, load_history
from sql_semantic_ledger.code_sources import scan_repository

ROOT = Path(__file__).resolve().parents[1]
TOY = ROOT / 'examples' / 'toy_shop'
HISTORY = TOY / 'history.sql'
APP = TOY / 'app'
ANSWERS = TOY / 'answers.jsonl'
EPS = 'E0=1,E1=0.6,E2=0.3,E3=0.05,E4=0.02'
PAID = ['orders.is_test = 0', "orders.status = 'paid'"]


def _load_make_db():
    spec = importlib.util.spec_from_file_location('toy_shop_make_db', TOY / 'make_db.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


make_db = _load_make_db()


def run(capsys, *args):
    """Run ``ledger args...`` in-process; return (exit code, stdout, stderr)."""
    code = main([str(a) for a in args])
    out, err = capsys.readouterr()
    return code, out, err


def find(ledger, kind, expression, conditions=()):
    matches = [d for d in ledger
               if d.kind == kind and d.expression == expression and sorted(d.conditions) == sorted(conditions)]
    assert len(matches) == 1, [(d.kind.value, d.expression, d.conditions) for d in ledger]
    return matches[0]


def evidence_count(path):
    return sum(len(d.evidence) for d in Ledger.load(path))


def subprocess_env(**extra):
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join(filter(None, [str(ROOT / 'src'), env.get('PYTHONPATH')]))
    env.update(extra)
    return env


@pytest.fixture
def shop(tmp_path):
    return make_db.build_database(tmp_path / 'shop.sqlite')


@pytest.fixture
def ledger_path(tmp_path, capsys):
    """A ledger induced from the toy history and app code (all E1)."""
    path = tmp_path / 'ledger.json'
    code, _, err = run(capsys, 'induce', HISTORY, '--code-root', APP, '-o', path)
    assert code == 0, err
    return path


@pytest.fixture
def checked(ledger_path, shop, capsys):
    """The induced ledger after `ledger check` against the toy database."""
    code, _, _ = run(capsys, 'check', ledger_path, '--db', shop)
    assert code == 1     # the stale 'complete' definitions fail their checks
    return ledger_path


@pytest.fixture
def certified(checked, capsys):
    """The checked ledger with the paid-revenue metric certified (E3); returns (path, metric id)."""
    paid = find(Ledger.load(checked), Kind.METRIC, 'SUM(orders.amount)', PAID)
    code, _, err = run(capsys, 'certify', checked, paid.id, '--by', 'finance lead')
    assert code == 0, err
    return checked, paid.id


# ---------------------------------------------------------------------------------------------- features, help
def test_features_prints_the_feature_table_in_both_languages(capsys):
    code, out, _ = run(capsys, 'features')
    assert code == 0 and out == table('en') + '\n'
    code, out, _ = run(capsys, 'features', '--lang', 'ko')
    assert code == 0 and out == table('ko') + '\n' and '미구현' in out


def test_help_and_version_exit_zero(capsys):
    code, out, _ = run(capsys, '--help')
    assert code == 0 and 'induce' in out and 'scan-code' in out and 'plan' in out
    code, out, _ = run(capsys, '--version')
    assert code == 0 and 'sql-semantic-ledger' in out
    code, out, _ = run(capsys, 'decide', '--help')
    assert code == 0 and '--eps' in out


@pytest.mark.parametrize('args', [[], ['nosuch'], ['features', '--lang', 'fr'], ['induce', str(HISTORY)],
                                  ['show'], ['decide', 'ledger.json'], ['plan', 'ledger.json']])
def test_usage_errors_exit_2(capsys, args):
    code, out, err = run(capsys, *args)
    assert code == 2 and out == '' and 'error' in err


# ---------------------------------------------------------------------------------------------- scan-code, induce
def test_scan_code_writes_jsonl_that_induce_reads_back(tmp_path, capsys):
    code, out, err = run(capsys, 'scan-code', APP)
    assert code == 0 and 'found 5 SQL queries in 2 files' in err
    rows = [json.loads(line) for line in out.splitlines()]
    assert [r['ref'] for r in rows] == ['orders.js:12', 'orders.js:24', 'orders.js:37', 'report.py:10', 'report.py:17']
    assert all(set(r) == {'sql', 'id', 'kind', 'ref'} and r['kind'] == 'code' for r in rows)
    assert rows[0]['id'] == 'orders.js:12:28' and rows[2]['sql'].endswith('LIMIT ?')   # ${limit} -> ?

    queries = tmp_path / 'out' / 'queries.jsonl'
    code, out, _ = run(capsys, 'scan-code', APP, '-o', queries)
    assert code == 0 and 'wrote' in out and queries.read_text(encoding='utf-8').splitlines() == \
        [json.dumps(r, ensure_ascii=False) for r in rows]

    code, out, _ = run(capsys, 'induce', queries, '-o', tmp_path / 'code.json')
    assert code == 0 and f'read 5 queries from {queries} (5 code)' in out
    led = Ledger.load(tmp_path / 'code.json')
    metric = find(led, Kind.METRIC, 'SUM(orders.amount)', PAID)
    assert metric.sources == ['code'] and metric.support == 3 and metric.grade == Grade.E1


def test_induce_writes_e1_definitions_from_history_and_code(ledger_path, capsys):
    led = Ledger.load(ledger_path)
    assert len(led) == 15 and all(d.grade == Grade.E1 for d in led)
    paid = find(led, Kind.METRIC, 'SUM(orders.amount)', PAID)
    assert paid.support == 8 and paid.sources == ['code', 'history'] and paid.source == 'code+history'
    assert 'q17' not in {e.ref for e in paid.evidence}           # a re-run of q1 is not independent support
    assert {d.kind for d in led} == {Kind.MEASURE, Kind.FILTER, Kind.JOIN, Kind.METRIC}


def test_induce_merges_into_an_existing_ledger(certified, capsys):
    path, paid_id = certified
    before = evidence_count(path)
    code, out, _ = run(capsys, 'induce', HISTORY, '--code-root', APP, '-o', path)
    assert code == 0 and f'updated {path} (+0 new): 15 definitions' in out
    assert evidence_count(path) == before                       # the same queries add no new evidence
    assert Ledger.load(path).get(paid_id).grade == Grade.E3      # the certification is kept
    assert not path.with_name(path.name + '.tmp').exists()


def test_induce_options(tmp_path, capsys):
    code, out, _ = run(capsys, 'induce', HISTORY, '--no-metrics', '--min-support', 3, '-o', tmp_path / 'a.json')
    assert code == 0 and f'read 17 queries from {HISTORY} (17 history)' in out
    led = Ledger.load(tmp_path / 'a.json')
    assert len(led) == 4 and all(d.kind != Kind.METRIC and d.support >= 3 for d in led)
    code, _, _ = run(capsys, 'induce', HISTORY, '--dialect', 'sqlite', '-o', tmp_path / 'b.json')
    led = Ledger.load(tmp_path / 'b.json')
    assert code == 0 and led and all(d.sources == ['history'] for d in led)


def test_induce_splits_a_history_with_the_dialect(tmp_path, capsys):
    history = tmp_path / 'h.sql'
    history.write_text("# q1: paid; checked\nSELECT SUM(amount) FROM orders WHERE status = 'paid';\n"
                       "# q2: paid again; checked\nSELECT SUM(amount) FROM orders WHERE status = 'paid' AND amount > 0;\n",
                       encoding='utf-8')
    code, out, _ = run(capsys, 'induce', history, '--dialect', 'mysql', '-o', tmp_path / 'l.json')
    assert code == 0 and 'read 2 queries' in out


def test_verbose_logs_to_stderr_and_cleans_up(tmp_path, capsys):
    logger = logging.getLogger('sql_semantic_ledger')
    handlers, level = list(logger.handlers), logger.level
    code, _, err = run(capsys, 'induce', HISTORY, '-o', tmp_path / 'l.json', '-v')
    assert code == 0 and 'INFO sql_semantic_ledger.induce' in err
    assert logger.handlers == handlers and logger.level == level
    code, _, err = run(capsys, '-vv', 'features')
    assert code == 0


# ---------------------------------------------------------------------------------------------- check
def test_check_records_evidence_and_exits_1_when_a_check_fails(ledger_path, shop, capsys):
    code, out, _ = run(capsys, 'check', ledger_path, '--db', shop)
    assert code == 1
    assert 'checked 15 definitions' in out and '29 of 31 checks passed' in out
    assert 'grades now: E0 2, E1 0, E2 13, E3 0, E4 0' in out
    assert 'falsified: orders_status_eq_complete, sum_orders_amount_where_orders_status_eq_complete' in out
    assert out.count('FAIL  ') == 2 and out.count('PASS  ') == 29
    led = Ledger.load(ledger_path)
    assert find(led, Kind.FILTER, "orders.status = 'complete'").falsified
    assert find(led, Kind.METRIC, 'SUM(orders.amount)', PAID).grade == Grade.E2


def test_check_json_and_all_passing_ledger(tmp_path, shop, capsys):
    path = tmp_path / 'ok.json'
    good = [q for q in load_history(HISTORY) if 'complete' not in q.sql]
    induce(good).save(path)
    code, out, _ = run(capsys, 'check', path, '--db', shop, '--json')
    report = json.loads(out)
    assert code == 0 and report['failed'] == 0 and report['passed'] == len(report['results']) > 0
    assert set(report['grades'].values()) == {'E2'}
    assert {'definition_id', 'name', 'check', 'passed', 'detail'} == set(report['results'][0])


def test_check_refuses_a_wrong_database_and_records_nothing(ledger_path, tmp_path, capsys):
    before = evidence_count(ledger_path)
    other = tmp_path / 'other.sqlite'
    with closing(sqlite3.connect(str(other))) as conn:
        conn.execute('CREATE TABLE products (id INTEGER)')
        conn.commit()
    not_db = tmp_path / 'notes.sqlite'
    not_db.write_text('not a database', encoding='utf-8')
    for db, message in ((other, 'none of the ledger tables'), (not_db, 'not a usable SQLite database'),
                        (tmp_path / 'missing.sqlite', 'database file not found')):
        code, out, err = run(capsys, 'check', ledger_path, '--db', db)
        assert code == 2 and message in err and out == ''
    assert evidence_count(ledger_path) == before


def test_check_opens_the_database_read_only(ledger_path, shop, capsys):
    before = shop.read_bytes()
    run(capsys, 'check', ledger_path, '--db', shop)
    assert shop.read_bytes() == before


def test_check_empty_ledger(tmp_path, shop, capsys):
    path = tmp_path / 'empty.json'
    Ledger().save(path)
    code, out, _ = run(capsys, 'check', path, '--db', shop)
    assert code == 0 and 'no definitions to check' in out


# ---------------------------------------------------------------------------------------------- certify, show
def test_certify_by_id_or_name_and_reject(checked, capsys):
    led = Ledger.load(checked)
    paid = find(led, Kind.METRIC, 'SUM(orders.amount)', PAID)
    code, out, _ = run(capsys, 'certify', checked, paid.id, '--by', 'finance lead', '--note', 'matches the report')
    assert code == 0 and out.strip() == f'certified {paid.name} [{paid.id}] by finance lead: E2 -> E3'
    code, out, _ = run(capsys, 'certify', checked, 'JOIN_CUSTOMERS_ORDERS', '--by', 'data engineer')
    assert code == 0 and 'join_customers_orders' in out and 'E2 -> E3' in out
    code, out, _ = run(capsys, 'certify', checked, 'join_customers_orders', '--by', 'auditor', '--reject')
    assert code == 0 and 'recorded rejection of join_customers_orders' in out and 'E3 -> E0' in out
    led = Ledger.load(checked)
    assert led.get(paid.id).grade == Grade.E3 and led.get(paid.id).current_evidence()[-1].detail == 'matches the report'
    assert [e.type for e in led.events].count('certify') == 3


@pytest.mark.parametrize('args, message', [
    (['nope', '--by', 'x'], "no definition with id or name 'nope'"),
    (['join_customers_orders', '--by', '  '], '--by needs the name'),
])
def test_certify_errors(checked, capsys, args, message):
    before = evidence_count(checked)
    code, _, err = run(capsys, 'certify', checked, *args)
    assert code == 2 and message in err
    assert evidence_count(checked) == before


def test_certify_ambiguous_name(tmp_path, capsys):
    from sql_semantic_ledger import Definition

    led = Ledger()
    led.add(Definition('revenue', Kind.MEASURE, 'SUM(orders.amount)'))
    led.add(Definition('revenue', Kind.MEASURE, 'SUM(orders.net_amount)'))
    path = tmp_path / 'l.json'
    led.save(path)
    code, _, err = run(capsys, 'certify', path, 'revenue', '--by', 'x')
    assert code == 2 and "2 definitions are named 'revenue'" in err


def test_show_lists_filters_and_prints_json(certified, capsys):
    path, paid_id = certified
    code, out, _ = run(capsys, 'show', path)
    lines = out.splitlines()
    assert code == 0 and lines[0].startswith('15 definitions in') and '(E0 2, E1 0, E2 12, E3 1, E4 0)' in lines[0]
    assert lines[1].startswith(f'E3  metric   {paid_id}') and 'support 8 (code, history)' in lines[1]
    assert lines[2].strip() == "SUM(orders.amount) WHERE orders.is_test = 0 AND orders.status = 'paid'"
    assert lines[-2].startswith('E0') and 'falsified' in lines[-2]
    code, out, _ = run(capsys, 'show', path, '--min-grade', 'E2', '--kind', 'metric')
    assert code == 0 and out.startswith('5 of 15 definitions') and 'kind metric, grade >= E2' in out
    code, out, _ = run(capsys, 'show', path, '--kind', 'join', '--json')
    rows = json.loads(out)
    assert code == 0 and [r['name'] for r in rows] == ['join_customers_orders'] and rows[0]['grade'] == 'E2'


def test_show_puts_an_or_condition_among_several_in_parentheses(tmp_path, capsys):
    history = tmp_path / 'history.sql'
    history.write_text("SELECT COUNT(*) FROM orders WHERE (status = 'paid' OR status = 'shipped') AND amount > 0;\n"
                       "SELECT status, COUNT(*) FROM orders WHERE amount > 0 AND (status = 'shipped' OR status = 'paid')"
                       " GROUP BY status;\n"
                       "SELECT COUNT(*) FROM orders WHERE status = 'paid' OR status = 'shipped';\n"
                       "SELECT status, COUNT(*) FROM orders WHERE status = 'shipped' OR status = 'paid' GROUP BY status;\n",
                       encoding='utf-8')
    path = tmp_path / 'ledger.json'
    assert run(capsys, 'induce', history, '-o', path)[0] == 0
    code, out, _ = run(capsys, 'show', path, '--kind', 'metric')
    assert code == 0
    assert "COUNT(*) WHERE orders.amount > 0 AND (orders.status = 'paid' OR orders.status = 'shipped')" in out
    assert "COUNT(*) WHERE orders.status = 'paid' OR orders.status = 'shipped'\n" in out   # one condition: as is


# ---------------------------------------------------------------------------------------------- decide
def test_decide_confirm_warn_reject_with_error_bounds(certified, capsys):
    path, paid_id = certified
    led = Ledger.load(path)
    join = find(led, Kind.JOIN, 'customers.id = orders.customer_id')
    stale = find(led, Kind.METRIC, 'SUM(orders.amount)', ["orders.status = 'complete'"])
    options = ['--eps', EPS, '--delta', '0.02', '--target', '0.1']

    code, out, _ = run(capsys, 'decide', path, paid_id, *options)
    assert code == 0 and out.startswith('CONFIRM (error bound 0.07)')
    assert '- error bound 0.07 = delta 0.02 + eps(E3) 0.05 <= target 0.1' in out

    code, out, _ = run(capsys, 'decide', path, paid_id, 'join_customers_orders', *options)
    assert code == 0 and out.startswith('WARN (error bound 0.37)') and 'below E3 -> warn' in out

    code, out, _ = run(capsys, 'decide', path, stale.id, paid_id, *options)
    assert code == 0 and out.startswith('REJECT (error bound 1)') and 'falsified by failed check executes' in out

    code, out, _ = run(capsys, 'decide', path, paid_id, join.id, paid_id, '--eps', EPS, '--json')
    decision = json.loads(out)
    assert decision['verdict'] == 'warn' and decision['bound'] == pytest.approx(0.35)
    assert decision['definitions'] == [paid_id, join.id] and decision['grades'] == {paid_id: 'E3', join.id: 'E2'}


def test_decide_target_downgrades_and_needs_eps(certified, capsys):
    path, paid_id = certified
    code, out, _ = run(capsys, 'decide', path, paid_id, '--eps', EPS, '--delta', '0.02', '--target', '0.05')
    assert code == 0 and out.startswith('WARN (error bound 0.07)') and '> target 0.05 -> warn' in out
    code, out, err = run(capsys, 'decide', path, paid_id, '--target', '0.05')
    assert code == 0 and out.startswith('CONFIRM\n') and 'take effect only with --eps' in err


def test_decide_partial_eps_counts_missing_grades_as_one(certified, capsys):
    path, paid_id = certified
    code, out, _ = run(capsys, 'decide', path, paid_id, 'join_customers_orders', '--eps', 'E3=0.05')
    assert code == 0 and 'error bound 1 (capped at 1)' in out


@pytest.mark.parametrize('eps, message', [('E9=0.1', 'unknown evidence grade'), ('E1=2', 'must be in [0, 1]'),
                                          ('E1', 'GRADE=RATE'), ('E1=x', 'not a number'), (',', 'GRADE=RATE'),
                                          ('E1=0.5,e1=0.4', 'more than once'), ('E1=0.5,E1=0.4', 'more than once')])
def test_decide_rejects_bad_eps(certified, capsys, eps, message):
    path, paid_id = certified
    code, out, err = run(capsys, 'decide', path, paid_id, '--eps', eps)
    assert code == 2 and out == '' and message in err


@pytest.mark.parametrize('args, message', [(['--delta', '1.5'], 'probability'), (['--target', 'high'], 'not a number')])
def test_decide_rejects_bad_numbers(certified, capsys, args, message):
    path, paid_id = certified
    code, _, err = run(capsys, 'decide', path, paid_id, *args)
    assert code == 2 and message in err


# ---------------------------------------------------------------------------------------------- plan
def test_answers_jsonl_ids_are_in_the_induced_ledger():
    led = induce(load_history(HISTORY) + scan_repository(APP))
    for line in ANSWERS.read_text(encoding='utf-8').splitlines():
        answer = json.loads(line)
        assert answer['definitions'] and all(i in led for i in answer['definitions']), answer
        assert led.get(answer['definitions'][0]).kind in (Kind.MEASURE, Kind.METRIC)


def test_plan_ranks_affected_answers_within_budget(certified, capsys):
    path, paid_id = certified
    code, out, _ = run(capsys, 'plan', path, '--answers', ANSWERS, '--changed', paid_id, '--changed-table', 'orders',
                       '--budget', 3)
    lines = out.splitlines()
    assert code == 0 and lines[0] == ('revalidation plan: 3 of 6 candidate answers '
                                      '(changed definitions or tables, budget 3)')
    assert [line.split()[1] for line in lines[1:]] == ['a2', 'a3', 'a6']
    assert 'score 1.110' in lines[1] and 'changed: definition' in lines[1]

    code, out, _ = run(capsys, 'plan', path, '--answers', ANSWERS, '--changed',
                       'sum_orders_amount_where_orders_is_test_eq_0_and_orders_status_eq_paid', '--json')
    report = json.loads(out)
    assert code == 0 and report['candidates'] == 2 and report['budget'] is None
    assert [i['answer_id'] for i in report['items']] == ['a2', 'a1']


def test_plan_without_changes_and_with_unaffected_changes(certified, capsys):
    path, _ = certified
    code, out, _ = run(capsys, 'plan', path, '--answers', ANSWERS, '--eps', 'E0=1,E2=0.25', '--json')
    report = json.loads(out)
    top = report['items'][0]
    assert code == 0 and report['candidates'] == 6 and len(report['items']) == 6
    assert top['answer_id'] == 'a6' and top['score'] == 1.0 and 'E0 (risk 1)' in top['reason']
    assert report['items'][1]['answer_id'] == 'a2' and report['items'][1]['score'] == pytest.approx(0.975)
    code, out, _ = run(capsys, 'plan', path, '--answers', ANSWERS, '--changed-table', 'payments', '--budget', 0)
    assert code == 0 and out.strip() == 'no stored answer (of 6) is affected by the changes'


@pytest.mark.parametrize('content, message', [
    ('{"id": "a1", "definitions": ["x"]}\n{broken\n', 'answers.jsonl:2: invalid JSON'),
    ('[1, 2]\n', 'answers.jsonl:1: expected a JSON object'),
    ('{"definitions": ["x"]}\n', 'needs an "id"'),
    ('{"id": "a1", "definitions": "x"}\n', 'not a single string'),
    ('{"id": "a1", "definitions": [], "weight": -1}\n', 'weight must be'),
])
def test_plan_reports_bad_answer_files(certified, tmp_path, capsys, content, message):
    path, _ = certified
    answers = tmp_path / 'answers.jsonl'
    answers.write_text(content, encoding='utf-8')
    code, _, err = run(capsys, 'plan', path, '--answers', answers)
    assert code == 2 and message in err


def test_plan_errors(certified, tmp_path, capsys):
    path, _ = certified
    code, _, err = run(capsys, 'plan', path, '--answers', tmp_path / 'none.jsonl')
    assert code == 2 and 'answers file not found' in err
    code, _, err = run(capsys, 'plan', path, '--answers', ANSWERS, '--changed', 'nope')
    assert code == 2 and "no definition with id or name 'nope'" in err
    code, _, err = run(capsys, 'plan', path, '--answers', ANSWERS, '--budget', '-1')
    assert code == 2 and 'integer >= 0' in err


# ---------------------------------------------------------------------------------------------- input errors
@pytest.mark.parametrize('content, message', [
    ('{broken', 'invalid JSON'),
    ('[]', 'expected a JSON object'),
    ('{"definitions": [{"kind": "metric"}]}', 'not a valid ledger file'),
    ('{"schema_version": 99, "definitions": []}', 'schema version 99'),
])
def test_bad_ledger_files_exit_2(tmp_path, capsys, content, message):
    path = tmp_path / 'ledger.json'
    path.write_text(content, encoding='utf-8')
    code, out, err = run(capsys, 'show', path)
    assert code == 2 and out == '' and message in err and err.startswith('ledger: error: ')
    assert len(err.strip().splitlines()) == 1


def test_missing_inputs_exit_2(tmp_path, capsys):
    cases = [(['show', tmp_path / 'none.json'], 'ledger file not found'),
             (['show', tmp_path], 'ledger path is not a file'),
             (['induce', tmp_path / 'none.sql', '-o', tmp_path / 'l.json'], 'history file not found'),
             (['induce', HISTORY, '--code-root', tmp_path / 'none', '-o', tmp_path / 'l.json'], 'code root not found'),
             (['scan-code', tmp_path / 'none'], 'code root not found'),
             (['induce', HISTORY, '--dialect', 'nosuch', '-o', tmp_path / 'l.json'], "Unknown dialect 'nosuch'"),
             (['induce', HISTORY, '--min-support', '0', '-o', tmp_path / 'l.json'], 'integer >= 1')]
    for args, message in cases:
        code, _, err = run(capsys, *args)
        assert code == 2 and message in err, (args, err)
    assert not (tmp_path / 'l.json').exists()


def test_unsupported_history_file_exits_2(tmp_path, capsys):
    history = tmp_path / 'history.csv'
    history.write_text('sql\nSELECT 1\n', encoding='utf-8')
    code, _, err = run(capsys, 'induce', history, '-o', tmp_path / 'l.json')
    assert code == 2 and 'unsupported history file' in err


# ---------------------------------------------------------------------------------------------- encodings, processes
def test_json_output_keeps_non_ascii_text(tmp_path, capsys):
    app = tmp_path / 'app'
    app.mkdir()
    (app / 'api.js').write_text("const q = `SELECT SUM(amount) FROM orders WHERE status = '결제완료'`;\n",
                                encoding='utf-8')
    code, out, _ = run(capsys, 'scan-code', app)
    assert code == 0 and "'결제완료'" in out and '\\u' not in out
    history = tmp_path / 'history.sql'
    history.write_text("SELECT SUM(amount) FROM orders WHERE status = '결제완료';\n"
                       "SELECT COUNT(*) FROM orders WHERE status = '결제완료';\n", encoding='utf-8')
    run(capsys, 'induce', history, '-o', tmp_path / 'l.json')
    assert "'결제완료'" in (tmp_path / 'l.json').read_text(encoding='utf-8')
    code, out, _ = run(capsys, 'show', tmp_path / 'l.json', '--kind', 'filter', '--json')
    assert code == 0 and json.loads(out)[0]['expression'] == "orders.status = '결제완료'" and '\\u' not in out


def test_process_exit_codes_and_utf8_json_on_a_non_utf8_console(tmp_path, shop):
    ledger = tmp_path / 'ledger.json'
    cli = [sys.executable, '-m', 'sql_semantic_ledger.cli']
    done = subprocess.run(cli + ['induce', str(HISTORY), '-o', str(ledger)], cwd=str(tmp_path),
                          env=subprocess_env(), capture_output=True)
    assert done.returncode == 0, done.stderr
    done = subprocess.run(cli + ['check', str(ledger), '--db', str(shop)], env=subprocess_env(), capture_output=True)
    assert done.returncode == 1
    done = subprocess.run(cli + ['show', str(tmp_path / 'none.json')], env=subprocess_env(), capture_output=True)
    assert done.returncode == 2 and done.stderr.startswith(b'ledger: error:')

    app = tmp_path / 'app'
    app.mkdir()
    (app / 'api.py').write_text("Q = \"SELECT COUNT(*) FROM orders WHERE status = '배송중'\"\n", encoding='utf-8')
    latin = subprocess_env(PYTHONIOENCODING='latin-1')
    done = subprocess.run(cli + ['scan-code', str(app)], env=latin, capture_output=True)
    assert done.returncode == 0 and json.loads(done.stdout.decode('utf-8'))['sql'].endswith("'배송중'")
    ascii_console = subprocess_env(PYTHONIOENCODING='ascii')
    done = subprocess.run(cli + ['features', '--lang', 'ko'], env=ascii_console, capture_output=True)
    assert done.returncode == 0 and done.stderr == b''
    assert done.stdout.decode('ascii').count('?') > 10 and '`model`' in done.stdout.decode('ascii')


def test_demo_runs_end_to_end(tmp_path):
    pytest.importorskip('yaml')
    demo = [sys.executable, 'examples/toy_shop/demo.py']        # the documented command, from the package root
    done = subprocess.run(demo, cwd=str(ROOT), env=subprocess_env(), capture_output=True, text=True,
                          encoding='utf-8', timeout=300)
    assert done.returncode == 0, done.stderr
    out = done.stdout
    for text in ('15 definitions are used by >= 2 independent queries or code sites',
                 '29 of 31 checks passed', 'E2 -> E3 (certified by finance lead)',
                 '-> CONFIRM (error bound 0.07)', '-> WARN (error bound 0.37)', '-> REJECT (error bound 1.00)',
                 '1. a1', '2. a2', 'finds 2 of 2 (recall 1.00)', 'certifies it again: E3',
                 'same definition (ids come from the normalized SQL): web_shop/active_customers, '
                 'mobile_app/active-customers', 'paid_revenue: conditions differ', '1 conflict(s):'):
        assert text in out, text

    done = subprocess.run(demo + ['--out', str(tmp_path / 'demo')], cwd=str(ROOT), env=subprocess_env(),
                          capture_output=True, text=True, encoding='utf-8', timeout=300)
    assert done.returncode == 0, done.stderr
    led = Ledger.load(tmp_path / 'demo' / 'ledger.json')
    paid = led.get('ae289fba8e7e')
    assert paid.version == 2 and paid.grade == Grade.E3
    assert (tmp_path / 'demo' / 'shop.sqlite').is_file()


@pytest.mark.parametrize('readme', ['README.md', 'README.ko.md'])
def test_readme_quick_start_runs(readme, tmp_path, monkeypatch, capsys):
    blocks = re.findall(r'```python\n(.*?)```', (ROOT / readme).read_text(encoding='utf-8'), flags=re.S)
    assert blocks, f'{readme} has no python block'
    shutil.copytree(str(TOY), str(tmp_path / 'examples' / 'toy_shop'))
    make_db.build_database(tmp_path / 'shop.sqlite')
    monkeypatch.chdir(tmp_path)
    exec(compile(blocks[0], readme, 'exec'), {'__name__': '__readme__'})
    assert capsys.readouterr().out.strip() == 'warn 0.37'


def test_readmes_list_the_generated_feature_tables():
    assert table('en') in (ROOT / 'README.md').read_text(encoding='utf-8')
    assert table('ko') in (ROOT / 'README.ko.md').read_text(encoding='utf-8')


def test_make_db_script_builds_the_database(tmp_path):
    target = tmp_path / 'db' / 'shop.sqlite'
    done = subprocess.run([sys.executable, str(TOY / 'make_db.py'), str(target)], capture_output=True, text=True)
    assert done.returncode == 0 and 'wrote' in done.stdout
    with closing(sqlite3.connect(str(target))) as conn:
        assert conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0] == len(make_db.ORDERS)
        assert conn.execute("SELECT COUNT(*) FROM orders WHERE status = 'complete'").fetchone()[0] == 0
    with pytest.raises(FileExistsError):
        make_db.build_database(target, overwrite=False)
    assert make_db.add_shipped_orders(target) == 3
