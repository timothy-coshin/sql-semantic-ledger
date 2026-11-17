# Lifecycle of Evidence-graded Definitions: Generation, Evaluation, Revalidation (LEDGER)

[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE) ![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg) ![Status: pre-alpha](https://img.shields.io/badge/status-pre--alpha-orange.svg)

[English](README.md) | [한국어 (Korean)](README.ko.md)

> **Status: pre-alpha research preview (0.1.0a1).** What is implemented is tested, but APIs may still change.
> Features marked *planned* in the [feature table](#features) are not implemented; where they have an API stub,
> calling it raises `NotImplementedYet`.

**sql-semantic-ledger** is the reference implementation of **LEDGER**,
[*Lifecycle of Evidence-graded Definitions: Generation, Evaluation, Revalidation*](#how-it-works). It keeps the
business definitions of a SQL database (measures such as `SUM(orders.amount)`, filters such as
`orders.status = 'paid'`, joins, and metrics that pair an aggregate with the conditions it applies under) in an
append-only ledger, together with the evidence behind each one. Definitions are induced from SQL usage histories
and from SQL strings in service code, graded E0-E4 by that evidence (independent uses, data checks, a person's
certification, counterexample checks), and used to confirm, warn about or reject answers with an upper bound on
their error rate; when definitions or data change, the stored answers they affect are ranked for revalidation.
Trust in a definition is computed from its evidence instead of being a "verified" flag.

## How it works

| Phase | What the code does | Modules |
|---|---|---|
| **Generation** (induce) | Normalizes SQL with sqlglot (aliases resolved, conditions in canonical form) and turns aggregates, row-level predicates, equality joins and aggregate + condition pairs into candidate definitions. A candidate used in the same form by at least two independent past queries or code sites is kept (E1). Data checks against the database raise it to E2 or falsify it (E0). Metric-store YAML files add definitions in the same normalized form (the same definition gets the same id, so its evidence merges), and same-named definitions that disagree across products are reported as conflicts. | `parse`, `induce`, `code_sources`, `multisource`, `checks` |
| **Evaluation** (trust) | Assembles SQL from ledger definitions by rules, then confirms, warns about or rejects the answer according to the grades of the definitions it uses, with the error bound below. Per-grade error rates can be estimated from judged samples. | `engine`, `grade`, `bound` |
| **Revalidation** | Indexes which stored answers use which definitions and tables, and ranks the answers affected by a change by weight x probability that a definition they use is wrong, within a budget. A simulation compares a plan with checking every affected answer. | `revalidate` |

Every change (new definition, evidence, certification, revision, expiry) is appended to the ledger's event log, and
revising a definition starts a new version whose grade is computed from its own evidence only.

## Install

The package is not on PyPI yet, so install it from the repository or from a source checkout:

```bash
pip install "sql-semantic-ledger @ git+https://github.com/timothy-coshin/sql-semantic-ledger.git"          # core (depends on sqlglot only)
pip install "sql-semantic-ledger[yaml] @ git+https://github.com/timothy-coshin/sql-semantic-ledger.git"    # + PyYAML, to read metric-store YAML files

git clone https://github.com/timothy-coshin/sql-semantic-ledger.git && cd sql-semantic-ledger
pip install -e ".[dev]"                                           # + pytest, to run the tests
```

A checkout also runs without installing, with `PYTHONPATH=src`. Python 3.9 or newer and sqlglot 30.8 or newer
within 30.x: definition ids are computed from sqlglot's normalized SQL, so the dependency is kept to the tested
range.

## Quick start

Build the toy shop database first: `python examples/toy_shop/make_db.py shop.sqlite`. Then, from the repository root:

```python
import sqlite3
from sql_semantic_ledger.checks import run_checks
from sql_semantic_ledger.code_sources import scan_repository
from sql_semantic_ledger.engine import assemble
from sql_semantic_ledger.grade import Policy, decide
from sql_semantic_ledger.induce import induce, load_history

ledger = induce(load_history('examples/toy_shop/history.sql') + scan_repository('examples/toy_shop/app'))
run_checks(ledger, sqlite3.connect('file:shop.sqlite?mode=ro', uri=True))   # E1 -> E2, or E0 if a check fails
paid = ledger.find(name='sum_orders_amount_where_orders_is_test_eq_0_and_orders_status_eq_paid')[0]
ledger.certify(paid.id, by='finance lead')                                  # a person's certification: E3
query = assemble(ledger, paid.id, group_by=['customers.region'])            # SQL assembled from the ledger
decision = decide([ledger.get(i) for i in query.definitions], Policy(target=0.1), delta=0.02,
                  eps={'E0': 1.0, 'E1': 0.6, 'E2': 0.3, 'E3': 0.05, 'E4': 0.02})   # illustrative rates
print(decision.verdict.value, round(decision.bound, 2))   # warn 0.37: the join it uses is only E2
```

`ledger.save('ledger.json')` writes the ledger with its event log; `Ledger.load` reads it back.

## Toy shop example

[`examples/toy_shop`](examples/toy_shop) holds a fictional shop: `make_db.py` (SQLite database with customers and
orders), `history.sql` (17 analyst queries), `app/orders.js` and `app/report.py` (SQL in an API handler and in a
report script), `metrics.yaml` (two products that define `paid_revenue` differently) and `answers.jsonl` (stored
answers with the definitions they used). `demo.py` runs the whole pipeline through the Python API:

```bash
PYTHONPATH=src python examples/toy_shop/demo.py            # add --out DIR to keep the database and ledger
```

It induces 15 definitions (all E1), runs 31 data checks (13 definitions reach E2; the two that use a stale status
value are falsified), certifies the paid-revenue metric (E3), decides three answers, plans a revalidation after a
data change and a definition revision, and reports the metric-store conflict. Excerpt:

```text
  Q: Paid revenue by region
     SQL: SELECT customers.region, SUM(orders.amount) AS "sum_orders_amount_where_..." FROM orders JOIN customers ON customers.id = orders.customer_id WHERE orders.is_test = 0 AND orders.status = 'paid' GROUP BY customers.region ORDER BY customers.region
     result: Busan 253.5, Daegu 254, Seoul 484.5
     -> WARN (error bound 0.37)
        ...
        join_customers_orders (E2): machine evidence only; supported by 6 past queries or code sites (...); data checks passed (...); below E3 -> warn
        error bound 0.37 = delta 0.02 + eps(E3) 0.05 + eps(E2) 0.3 > target 0.1
```

## Command line

```bash
python examples/toy_shop/make_db.py shop.sqlite
ledger scan-code examples/toy_shop/app -o code.jsonl          # SQL found in JS/TS/Python string literals
ledger induce examples/toy_shop/history.sql --code-root examples/toy_shop/app -o ledger.json
ledger check ledger.json --db shop.sqlite                     # exit code 1: two definitions are falsified
ledger show ledger.json --min-grade E2 --kind metric
ledger certify ledger.json ae289fba8e7e --by "finance lead" --note "matches the monthly report"
ledger decide ledger.json ae289fba8e7e join_customers_orders \
    --eps E0=1,E1=0.6,E2=0.3,E3=0.05,E4=0.02 --delta 0.02 --target 0.1
ledger plan ledger.json --answers examples/toy_shop/answers.jsonl \
    --changed ae289fba8e7e --changed-table orders --budget 3
ledger features --lang en
```

| Command | What it does |
|---|---|
| `features [--lang en\|ko]` | Prints the feature table below. |
| `induce HISTORY [--dialect D] [--min-support 2] [--code-root DIR] [--no-metrics] -o LEDGER.json` | Induces definitions from a `.sql` history (statements separated by `;`) or a `.jsonl` file, plus the SQL found under `--code-root`. Merges into `LEDGER.json` if it exists. |
| `scan-code ROOT [-o queries.jsonl] [--dialect D]` | Writes the SQL found in code as JSON lines `{"sql", "id", "kind": "code", "ref"}`, which `induce` reads back. |
| `check LEDGER.json --db DB.sqlite` | Runs the data checks against a SQLite file (opened read-only) and records them as evidence. Refuses a database that holds none of the ledger's tables. |
| `certify LEDGER.json ID --by NAME [--reject] [--note TEXT]` | Records a person's certification (E3) or rejection (E0). |
| `show LEDGER.json [--min-grade E1] [--kind metric]` | Lists definitions, strongest evidence first. |
| `decide LEDGER.json ID [ID ...] [--eps ...] [--delta P] [--target P]` | Confirms, warns about or rejects an answer that uses the given definitions; prints the error bound when `--eps` is given. |
| `plan LEDGER.json --answers answers.jsonl [--changed ID ...] [--changed-table T ...] [--budget N] [--eps ...]` | Ranks the stored answers affected by the changes for revalidation. |

Definitions can be named by id or by their unique name. `show`, `check`, `decide` and `plan` accept `--json`
(UTF-8, non-ASCII text kept as is). `-v` adds the traceback of unexpected errors and log messages on stderr (for
now only `induce` logs, a one-line summary); `-vv` adds debug details. Exit codes: 0 success, 1 `check` found a
failing check, 2 error (bad arguments, missing or invalid file, unknown definition). The CLI has no `revise` command
yet; use `Ledger.revise` from Python.

## Evidence grades and the answer rule

| Grade | Evidence | Assigned by |
|---|---|---|
| E0 | none (for example proposed by a model), or falsified (a failed check or a person's rejection) | fewer than two independent uses, a failed data or counterexample check on the current version, or a person's rejection (`Ledger.certify(..., passed=False)`, `ledger certify --reject`; the latest certification decision counts) |
| E1 | the same-form definition is used by at least two different past queries or code sites | `induce` (re-running one query counts once; a history query and a code site are different sources) |
| E2 | E1, and the data checks pass | `checks.run_checks`, `ledger check` |
| E3 | certified by a person | `Ledger.certify`, `ledger certify` |
| E4 | certified, and counterexample checks passed after certification (after a rejection, only checks after the new certification count) | `checks.counterexample_checks` |

Grades are not part of the ledger's state: they are always computed from the evidence about the current version of
a definition (`ledger.json` shows each grade at save time for reference only; a hand-edited grade is ignored).
`grade.decide` turns the grades of the definitions an answer uses into a verdict:

* **confirm** when every definition is certified (E3 or above, `Policy.confirm_min`);
* **warn** when a definition backed only by machine evidence (E1-E2) is mixed in;
* **reject** when a definition is E0 (no evidence, or falsified by a failed check or a person's rejection), when no
  definition matches, or when a definition is outside its validity period.

With per-grade error rates and `Policy(target=...)`, a confirmed answer whose error bound exceeds the target is
downgraded to warn. Each verdict comes with one reason per definition (grade, who certified or rejected it and when, which past
queries or code sites support it, which check falsified it).

## Error bound

For an answer to question *q* that uses the set of definitions *D(q)*:

$$P(\text{the answer to } q \text{ is wrong}) \le \min\Big(1,\ \delta + \sum_{d \in D(q)} \varepsilon\big(e(d)\big)\Big)$$

*e(d)* is the evidence grade of *d*, *ε(e)* the probability that a definition of grade *e* is wrong (for E3 and E4,
the error rate of certification), and *δ* the probability that the answer is wrong although every definition is
right (a wrong definition was chosen, or the SQL was assembled wrongly). `bound.error_bound` computes it; a grade
missing from `eps` counts as 1, so the bound stays valid. `bound.estimate_eps` turns judged samples, `(errors, n)`
per grade, into one-sided upper confidence bounds (exact Clopper-Pearson or Wilson), and `bound.compose` adds up
the bounds of the steps of a multi-step analysis (union bound). The bound is only as good as the error rates put
into it: the rates in the examples are illustrative, not measured. Per-grade rates should be estimated on
calibration databases and the error rate of confirmed answers, P(wrong | confirmed), checked on held-out
databases; turning this into a statistical guarantee is planned (see the feature table).

## Features

Generated with `features.table('en')` (also `ledger features`). Status `implemented` means the feature works and
is tested, `partial` a first, limited version (see the notes below), and `planned` not implemented: calling a
planned feature's API stub raises `NotImplementedYet`. *Area* names the lifecycle phase (Generation, Evaluation,
Revalidation) or the ledger and tooling; *Plan stage* places each feature in the five-year research roadmap
(stage 1 = years 1-3, stage 2 = years 4-5).

| Feature | Area | Status | Plan stage | Module |
|---|---|---|---|---|
| Append-only ledger of definitions, evidence and certifications | Ledger | implemented | Stage 1 | `model` |
| SQL normalization and candidate generation (aggregates, conditions, joins, metrics) | Generation | implemented | Stage 1, year 1 | `parse, induce` |
| Independent-support check: >= 2 different queries or code sites (E1) | Generation | implemented | Stage 1, year 1 | `induce` |
| Falsification against the database (E2) | Generation | implemented | Stage 1, years 1-2 | `checks` |
| Counterexample checks after certification (E4) | Evaluation | partial | Stage 1, years 2-3 | `checks` |
| Induction from SQL strings embedded in service code (JS/TS/Python) | Generation (stage 2) | implemented | Stage 2, year 4 | `code_sources` |
| Induction from ORM queries | Generation (stage 2) | planned | Stage 2, year 4 | `code_sources` |
| Static analysis that lifts UI JavaScript computations to definitions | Generation (stage 2) | planned | Stage 2, year 4 | `code_sources` |
| Metric-store (YAML) definitions and cross-source / cross-product conflict detection | Generation (stage 2) | partial | Stage 2, year 4 | `multisource` |
| Confirm / warn / reject an answer by the evidence grades of its definitions | Evaluation | implemented | Stage 1, year 2 | `grade` |
| Error-rate upper bound for an answer | Evaluation | implemented | Stage 1, years 2-3 | `bound` |
| Finite-sample upper confidence bounds for per-grade error rates | Evaluation | implemented | Stage 1, year 3 | `bound` |
| Composing error bounds over multi-step analyses (union bound) | Evaluation (stage 2) | implemented | Stage 2, year 5 | `bound` |
| Statistical guarantee for confirmed conclusions (threshold calibration) | Evaluation (stage 2) | planned | Stage 2, year 5 | `bound` |
| Rule-based SQL assembly from ledger definitions | Evaluation | partial | Stage 1, year 2 | `engine` |
| Choosing ledger definitions for a natural-language question (model) | Evaluation | planned | Stage 1, year 2 | `engine, propose` |
| Ledger server for data agents (MCP) | Evaluation (stage 2) | planned | Stage 2, years 4-5 | `agent_server` |
| Impact of definition or table changes on stored answers | Revalidation | implemented | Stage 1, year 3 | `revalidate` |
| Selective revalidation plan ranked by error rate x impact within a budget | Revalidation | implemented | Stage 1, year 3 | `revalidate` |
| Recall and cost against full revalidation | Revalidation | implemented | Stage 2, year 5 | `revalidate` |
| Collecting and judging real definition revisions (SemEvolve benchmark) | Revalidation | planned | Stage 1, year 3 | `-` |
| Command-line tool `ledger` | Tooling | implemented | - | `cli` |

Notes from the feature registry:

* Counterexample checks (partial): violating-row queries against the database; user-reported counterexamples are planned.
* Service-code SQL: string and template literals (and concatenated literals) that parse as queries over a table;
  interpolated values become parameters, and queries whose structure is built at run time are skipped.
* Metric store (partial): a minimal YAML schema; full dbt MetricFlow / metric-hub schemas are planned.
* SQL assembly (partial): one measure, conditions and group-by over the measure tables plus registered joins.
* The SemEvolve benchmark is a dataset to be collected, not an API, so it has no stub.

## Known limitations

* The answer engine assembles SQL from definitions the caller chooses; choosing definitions for a natural-language
  question is planned. Assembly uses inner joins only, does not correct fan-out through one-to-many joins, and does
  not support HAVING filters.
* SQL fragments are embedded in check queries unchanged, so they must be written in the database's dialect, and so
  must table and column names, quoted where needed (`"order items"`, as `induce` writes them). The CLI `check`
  command works with SQLite files; other databases can be checked from Python with any DB-API connection
  (`run_checks(ledger, conn, dialect=...)`).
* Statements of a `.sql` history get positional ids (q1, q2, ...). When several histories are merged into one ledger,
  use `.jsonl` records with unique ids: two different queries that share an id count as one use (support is never
  over-counted, and a query run again in a later history still counts once). Code sites are paths relative to the
  scanned root, so scan code from the same root each time.
* Code analysis reads SQL in string literals only; ORM queries and UI JavaScript computations are planned.

## Development

```bash
pip install -e ".[dev]"
python -m pytest -q          # includes the CLI on the toy shop data and a run of demo.py
```

## Citation

If you use this software, please cite it as:

```bibtex
@software{kim_sql_semantic_ledger,
  author  = {Kim, Wooyoung},
  title   = {sql-semantic-ledger: reference implementation of LEDGER (Lifecycle of Evidence-graded Definitions: Generation, Evaluation, Revalidation)},
  version = {0.1.0a1},
  year    = {2026},
  url     = {https://github.com/timothy-coshin/sql-semantic-ledger}
}
```

## Repository layout

| Path | Contents |
|---|---|
| `src/sql_semantic_ledger/` | the package: `model` (ledger), `parse` and `induce` (generation), `code_sources` and `multisource` (other sources), `checks`, `grade`, `bound`, `engine` (evaluation), `revalidate`, `cli` |
| `tests/` | pytest suite (runs the CLI on the toy data and the demo) |
| `examples/toy_shop/` | a small fictional shop: database builder, SQL history, service code, metric YAML, stored answers, `demo.py` |
| `CHANGELOG.md` | release notes |

## Contributing and contact

Bug reports and questions are welcome in the [issue tracker](https://github.com/timothy-coshin/sql-semantic-ledger/issues). Please include the command or code,
the SQL dialect, and the full error message. Pull requests should come with tests (`python -m pytest -q`).

## License and author

Apache License 2.0 (see [LICENSE](LICENSE)). Author: Wooyoung Kim <timothy@yonsei.ac.kr>.
