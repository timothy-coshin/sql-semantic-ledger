"""LEDGER end to end on a toy shop, through the Python API.

Run from the package root:

    PYTHONPATH=src python examples/toy_shop/demo.py [--out DIR]

Steps:
  1. induce   definitions from history.sql and the SQL strings in app/ (E1: >= 2 independent uses)
  2. check    them against the toy database (E2, or E0 when a check falsifies them)
  3. certify  one metric (E3)
  4. decide   three answers (confirm, warn, reject) with error bounds
  5. plan     which stored answers (answers.jsonl) to revalidate after a data and definition change
  6. detect   the conflicting definitions of two products in metrics.yaml (needs PyYAML)

The database and the ledger go to a temporary directory, or to DIR with --out (to explore them with the
`ledger` command afterwards). EPS are illustrative per-grade error rates, not measured ones.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

HERE = Path(__file__).resolve().parent
try:
    import sql_semantic_ledger  # noqa: F401
except ImportError:  # a source checkout without PYTHONPATH=src
    sys.path.insert(0, str(HERE.parents[1] / 'src'))

from sql_semantic_ledger import Definition, Kind, Ledger  # noqa: E402
from sql_semantic_ledger.checks import CheckResult, run_checks  # noqa: E402
from sql_semantic_ledger.code_sources import scan_repository  # noqa: E402
from sql_semantic_ledger.engine import assemble, assemble_sql  # noqa: E402
from sql_semantic_ledger.grade import Policy, decide  # noqa: E402
from sql_semantic_ledger.induce import induce, load_history  # noqa: E402
from sql_semantic_ledger.multisource import detect_conflicts, load_metric_yaml  # noqa: E402
from sql_semantic_ledger.revalidate import AnswerRecord, DependencyIndex, plan, simulate  # noqa: E402

EPS = {'E0': 1.0, 'E1': 0.6, 'E2': 0.3, 'E3': 0.05, 'E4': 0.02}   # illustrative, not measured
DELTA = 0.02                                                      # error of choosing/assembling definitions
TARGET = 0.10                                                     # a confirmed answer needs bound <= TARGET
PAID = ["orders.is_test = 0", "orders.status = 'paid'"]
PAID_OR_SHIPPED = ["orders.is_test = 0", "orders.status IN ('paid', 'shipped')"]


def _load_make_db() -> Any:
    """Import make_db.py from this directory (works however the demo is started)."""
    spec = importlib.util.spec_from_file_location('toy_shop_make_db', HERE / 'make_db.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


make_db = _load_make_db()


# ---------------------------------------------------------------------------------------------- helpers
def section(title: str) -> None:
    print()
    print(title)
    print('-' * len(title))


def connect(db: Path, read_only: bool = True) -> sqlite3.Connection:
    if read_only:
        return sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)
    return sqlite3.connect(str(db))


def ordered(definitions: Any) -> List[Definition]:
    rank = {Kind.METRIC: 0, Kind.MEASURE: 1, Kind.FILTER: 2, Kind.JOIN: 3, Kind.DIMENSION: 4}
    return sorted(definitions, key=lambda d: (rank[d.kind], d.name, d.id))


def find(ledger: Ledger, kind: Kind, expression: str, conditions: Sequence[str] = ()) -> Definition:
    """The induced definition with this kind, expression and conditions."""
    for d in ledger:
        if d.kind == kind and d.expression == expression and sorted(d.conditions) == sorted(conditions):
            return d
    raise LookupError(f'not induced: {kind.value} {expression} WHERE {" AND ".join(conditions)}')


def sql_text(d: Definition) -> str:
    return d.expression + (' WHERE ' + ' AND '.join(d.conditions) if d.conditions else '')


def value_text(value: Any) -> str:
    if value is None:
        return 'NULL'
    if isinstance(value, float):
        return f'{round(value, 2):g}'
    return str(value)


def rows_text(rows: Sequence[Sequence[Any]]) -> str:
    if len(rows) == 1 and len(rows[0]) == 1:
        return value_text(rows[0][0])
    return ', '.join(' '.join(value_text(v) for v in row) for row in rows) or '(no rows)'


def run_answer(ledger: Ledger, conn: sqlite3.Connection, answer: Dict[str, Any]) -> List[tuple]:
    """Recompute a stored answer: its first definition is the measure, FILTER definitions are applied."""
    measure, *others = answer['definitions']
    filters = [i for i in others if ledger.get(i).kind == Kind.FILTER]
    return conn.execute(assemble_sql(ledger, measure, filters, answer.get('group_by', []))).fetchall()


# ---------------------------------------------------------------------------------------------- the steps
def step_induce() -> Ledger:
    history = load_history(HERE / 'history.sql')
    code = scan_repository(HERE / 'app')
    ledger = induce(history + code)
    section('1. Induce definitions from the SQL usage history and the service code')
    print(f'{len(history)} past queries (history.sql) and {len(code)} SQL strings in app/ '
          f'({", ".join(sorted({r.ref.rsplit(":", 1)[0] for r in code}))})')
    print(f'{len(ledger)} definitions are used by >= 2 independent queries or code sites:')
    for d in ordered(ledger):
        print(f'  {d.grade.name}  {d.kind.value:<7}  {d.name}  (support {d.support}: {", ".join(d.sources)})')
    return ledger


def step_check(ledger: Ledger, db: Path) -> None:
    with closing(connect(db)) as conn:
        results = run_checks(ledger, conn)
    by_definition: Dict[str, List[CheckResult]] = {}
    for r in results:
        by_definition.setdefault(r.definition_id, []).append(r)
    section('2. Check the definitions against the database')
    for d in ordered(ledger):
        checks = by_definition.get(d.id, [])
        print(f'  {d.grade.name}  {d.name}: ' + ', '.join(f'{r.name} {"ok" if r.passed else "FAILED"}' for r in checks))
        for r in checks:
            if not r.passed:
                print(f'        {r.detail}')
    failed = sum(not r.passed for r in results)
    print(f'{len(results) - failed} of {len(results)} checks passed; a failed check falsifies the definition (E0)')


def step_certify(ledger: Ledger) -> Definition:
    paid = find(ledger, Kind.METRIC, 'SUM(orders.amount)', PAID)
    before = paid.grade
    ledger.certify(paid.id, by='finance lead', note='matches the monthly finance report')
    section('3. Certify one metric')
    print(f'  {paid.name}')
    print(f'    {sql_text(paid)}')
    print(f'    {before.name} -> {paid.grade.name} (certified by finance lead)')
    return paid


def step_decide(ledger: Ledger, db: Path, paid: Definition) -> None:
    complete = find(ledger, Kind.METRIC, 'SUM(orders.amount)', ["orders.status = 'complete'"])
    questions = [('Paid revenue so far', paid.id, []),
                 ('Paid revenue by region', paid.id, ['customers.region']),
                 ('Revenue on the old dashboard', complete.id, [])]
    section('4. Decide answers: confirm / warn / reject, with error bounds')
    print(f'eps = {EPS} (illustrative), delta = {DELTA}, target = {TARGET}')
    with closing(connect(db)) as conn:
        for question, measure_id, group_by in questions:
            query = assemble(ledger, measure_id, group_by=group_by)
            rows = conn.execute(query.sql).fetchall()
            decision = decide([ledger.get(i) for i in query.definitions], Policy(target=TARGET), eps=EPS, delta=DELTA)
            print(f'  Q: {question}')
            print(f'     SQL: {" ".join(query.sql.split())}')
            print(f'     result: {rows_text(rows)}')
            print(f'     -> {decision.verdict.value.upper()} (error bound {decision.bound:.2f})')
            for reason in decision.reasons:
                print(f'        {reason}')


def step_revalidate(ledger: Ledger, db: Path, paid: Definition) -> None:
    lines = (HERE / 'answers.jsonl').read_text(encoding='utf-8').splitlines()
    answers = [json.loads(line) for line in lines if line.strip()]
    index = DependencyIndex(AnswerRecord.from_dict(a) for a in answers)
    with closing(connect(db)) as conn:
        stored = {a['id']: run_answer(ledger, conn, a) for a in answers}

    added = make_db.add_shipped_orders(db)
    ledger.revise(paid.id, conditions=PAID_OR_SHIPPED, note="'shipped' orders are paid orders too")
    section('5. Revalidate stored answers after a data change and a definition change')
    print(f'{len(answers)} stored answers (answers.jsonl). Change: {added} orders with the new status '
          f"'shipped' arrive, and {paid.name} is revised:")
    print(f'    {sql_text(paid)}  (version {paid.version}, now {paid.grade.name}: no evidence yet)')
    candidates = plan(ledger, index, changed_definitions=[paid.id], changed_tables=['orders'], eps=EPS)
    chosen = candidates[:3]
    questions = {a['id']: a['question'] for a in answers}
    print(f'plan for {len(candidates)} candidate answers with budget 3 '
          f'(score = weight x P(a definition the answer uses is wrong)):')
    for rank, item in enumerate(chosen, start=1):
        print(f'  {rank}. {item.answer_id} {questions[item.answer_id]!r}  score {item.score:.2f}')
        print(f'       {item.reason}')

    with closing(connect(db)) as conn:
        current = {a['id']: run_answer(ledger, conn, a) for a in answers}
    wrong = sorted(i for i in stored if stored[i] != current[i])
    result = simulate(chosen, wrong, [item.answer_id for item in candidates])
    print(f'recomputing every candidate shows that {", ".join(wrong) or "none"} changed; the plan checks '
          f'{result["cost"]} of {result["candidates"]} answers (cost ratio {result["cost_ratio"]:.2f}) and finds '
          f'{result["found"]} of {result["wrong"]} (recall {result["recall"]:.2f})')
    for answer_id in wrong:
        print(f'    {answer_id}: {rows_text(stored[answer_id])}  ->  {rows_text(current[answer_id])}')

    with closing(connect(db)) as conn:
        checks = run_checks(ledger, conn, [paid.id])
    ledger.certify(paid.id, by='finance lead', note='paid or shipped, test orders excluded')
    print(f'data checks of version {paid.version}: '
          + ', '.join(f'{r.name} {"ok" if r.passed else "FAILED"}' for r in checks)
          + f'; the finance lead certifies it again: {paid.grade.name}')


def step_conflicts() -> None:
    section('6. Detect conflicting definitions across products (metrics.yaml)')
    try:
        store = load_metric_yaml(HERE / 'metrics.yaml')
    except ImportError as error:
        print(f'  skipped: {error}')
        return
    same_id: Dict[str, List[str]] = {}
    for d in store:
        print(f'  {d.meta.get("product", ""):<10}  {d.name:<16}  [{d.id}]  {sql_text(d)}')
        same_id.setdefault(d.id, []).append(f'{d.meta.get("product", "")}/{d.name}')
    for entries in same_id.values():
        if len(entries) > 1:
            print(f'same definition (ids come from the normalized SQL): {", ".join(entries)}')
    conflicts = detect_conflicts(store)
    print(f'{len(conflicts)} conflict(s):')
    for c in conflicts:
        print(f'  {c.name}: {", ".join(c.kinds)} differ')
        for source, text in zip(c.sources, c.expressions):
            print(f'    {source:<16}  {text}')


def run(work: Path) -> Ledger:
    """Run all steps with the database and ledger in ``work``; returns the final ledger."""
    db = make_db.build_database(work / 'shop.sqlite')
    ledger = step_induce()
    step_check(ledger, db)
    paid = step_certify(ledger)
    step_decide(ledger, db, paid)
    step_revalidate(ledger, db, paid)
    step_conflicts()
    ledger.save(work / 'ledger.json')
    return ledger


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description='LEDGER end to end on a toy shop.')
    parser.add_argument('--out', metavar='DIR', help='keep shop.sqlite and ledger.json in DIR')
    args = parser.parse_args(argv)
    if args.out:
        work = Path(args.out)
        work.mkdir(parents=True, exist_ok=True)
        ledger = run(work)
        print(f'\nkept {work / "shop.sqlite"} and {work / "ledger.json"} ({len(ledger.events)} ledger events); '
              f'try: ledger show {work / "ledger.json"} --min-grade E2')
    else:
        with tempfile.TemporaryDirectory() as tmp:
            run(Path(tmp))
    return 0


if __name__ == '__main__':
    sys.exit(main())
