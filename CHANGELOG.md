# Changelog

## 0.1.0a1 (unreleased)

First public preview of the LEDGER reference implementation (pre-alpha research preview).

- Ledger (`model`): business definitions, evidence, evidence grades E0-E4 computed from evidence, certification
  records, versions, validity periods, append-only event log, JSON persistence.
- 만들기 (induce; `parse`, `induce`, `code_sources`, `multisource`): SQL normalization and candidate definitions
  (measures, filters, joins, metrics) from SQL usage histories and from SQL string and template literals in
  JS/TS/Python code (interpolated values become parameters); independent-support check (E1; a query run again, also
  in a later history merged into the same ledger, counts once); minimal metric-store YAML schema, normalized like
  induced definitions so that the same definition gets the same id, and cross-product conflict detection (partial).
- Checks (`checks`): data checks against a DB-API connection (E2, or falsification to E0, e.g. when a filter or a
  metric's conditions select no rows); counterexample checks after certification (E4, partial).
- 믿기 (trust; `grade`, `bound`, `engine`): confirm / warn / reject rule with per-definition reasons; error-rate upper
  bound; Clopper-Pearson and Wilson per-grade error estimates; union bound over the steps of an analysis; rule-based
  SQL assembly from ledger definitions (partial: inner joins, no HAVING).
- 고치기 (revalidate; `revalidate`): dependency index, change impact, budgeted revalidation plan, recall/cost
  simulation.
- Table and column names are SQL text in the database's dialect, quoted only where needed (`"order items"`,
  `"Unit Price"`); induction, data checks, SQL assembly and revalidation all read them that way, and match table
  names case-insensitively.
- Command-line tool `ledger`: `features`, `induce`, `scan-code`, `check`, `certify`, `show`, `decide`, `plan`
  (`--json` output for `show`, `check`, `decide` and `plan`).
- Example `examples/toy_shop`: database builder, usage history, service code, metric store with a conflict, stored
  answers and an end-to-end demo (`demo.py`).
- README in English and Korean.
- Depends on sqlglot 30.8 or newer within 30.x (tested with 30.8-30.32): definition ids and canonical SQL come
  from sqlglot's parse trees, and 30.8 added the `NOT LIKE` form the normalizer relies on.
- Planned and clearly marked (API stubs raise `NotImplementedYet`): ORM and UI-JavaScript analysis, full dbt MetricFlow
  / metric-hub schemas, MCP ledger server, statistical guarantee (threshold calibration), choosing definitions for a
  natural-language question. The SemEvolve revision benchmark is planned as a dataset.
