# Architecture and Data Contracts

## Execution flow

1. `setup_workspace.py` checks the canonical source inventory in `originals/` and invokes `prep_inputs.py`.
2. `prep_inputs.py` removes exact duplicate schedule rows (excluding the export-only index column), records duplicates, and materializes runtime workbooks under `input/`.
3. The experiment runners load source rule/code files from `originals/` and prepared schedule/pairing data from `input/`.
4. `mgt_effective.py` filters MGT rules for each flight's local arrival date and then calls the supplied `originals/MGT.py` implementation.
5. `originals/pairings_builder.py` generates pairing candidates; the runners check the candidates and write run outputs under `output/`.
6. `independent_validator.py` re-implements legality checks from source rule data to provide a separate check.
7. `revenue_audit.py` audits route/equipment/weekday revenue joins. `objective_spec.py` validates a proposed four-component objective contract; `objective_layer_readiness.py` reports whether the production solver is available and decomposes saved coverage-MIP baseline logs.

## File ownership

- Treat `originals/` as source-of-truth. Do not write generated files into it.
- Treat `input/` and `output/` as generated directories. Recreate input with `python setup_workspace.py`; do not commit either directory.
- Do not load a root-level duplicate when a canonical source file exists in `originals/`.
- Keep pinned regression data under `tests/fixtures/`, and historical analytical evidence under `reports/baseline/`.
- The archived implementation at `archive/coverage_mip.py` is retained only to document the generic coverage baseline. It is not the missing production solver.

## Important domain contracts

- MGT date intervals are inclusive. The current workbook expires on 27 March 2027, while the schedule includes 28 March 2027. Fail closed until the source owner supplies the correct rule.
- The current `Freq` interpretation is ISO weekdays 1–7, with blank meaning daily. This still needs data-owner confirmation.
- Aircraft-group mappings and a 72-hour maximum layover are demo assumptions, not confirmed airline rules.
- Revenue market normalization maps both directions of a hub-spoke route to the same hub-market key. This requires business confirmation. Equipment is exact-match only; no aircraft crosswalk is inferred.
- An uncovered-flight slack variable is not a cancellation decision. Selected pairing count is not distinct crew-set count.
- An OPTIMAL solution from the coverage-only MIP is optimal only for that simplified mathematical model, not proof of operational feasibility.

## Baseline facts retained for regression/audit

- The source schedule has 19,245 exported rows, 7 exact duplicates, and 19,238 unique schedule rows over 155 calendar days.
- The historical one-week pairing cross-check found 36 invalid pairings in both validators, with matching invalid sets.
- The historical full-horizon cross-check found 997 vs 998 invalid pairings; the additional independent-validator finding is an MGT rule missing at the expiry boundary.
- Historical coverage-MIP logs are retained at `logs/mip_1W.log` and `logs/mip_155D.log`. The full-horizon log is historically labelled 166 days despite spanning 155 calendar days.

See `OBJECTIVE_LAYER_READINESS.md` for the detailed revenue audit and four-term objective blockers.
