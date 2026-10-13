# Recovery deliverable status and data requirements

## What is currently demonstrated

The repository contains a bounded joint operate/cancel prototype and independent validation/export scripts. The saved one-week CI result reported 825 scheduled flights, 570 operated, 255 cancelled/uncovered, and 249 selected pairings. This is 69.09% operated coverage. It reported zero aircraft-validation violations and zero crew-coverage errors, but aircraft resources were provisional `LINE_n` proxies rather than verified tail registrations. Treat this as a research baseline, not an operationally accepted plan.

The one-month run previously failed to find a feasible incumbent within its short solve budget. The 154-day run was blocked by missing/conflicting effective MGT rules. These are different failure modes: increasing solve time can help the first; only authoritative rule data can resolve the second.

## Re-run with the longer CI budget

```bash
python setup_workspace.py
python -m pytest -q tests
python scripts/run_one_week_joint_recovery.py --start-date 2026-10-25 --days 7 --time-limit-seconds 120 --skip-pricing
python scripts/run_joint_recovery_horizons.py --time-limit-seconds 120
```

The workflow uploads workbooks, CSV decision tables, JSON summaries, and validation evidence as the `recovery-audit` artifact. A solver status of `OPTIMAL` only applies to the restricted set of independently validated pairing columns used by this prototype; it does not prove global optimality over all possible pairings.

## Minimum external data needed to remove the blockers

1. **MGT:** corrected authoritative rows for the nine missing/conflicting service-date rules, with effective dates, frequency semantics, airports, and equipment/subfleet scope. Do not extrapolate expired rules.
2. **Aircraft:** registered tail ID, equipment/subfleet, initial station, and availability timestamp for each aircraft.
3. **Compatibility:** owner-approved mapping from schedule equipment codes to aircraft/subfleet types.
4. **Objective:** confirmed lexicographic priority and definitions for cancellation, retiming, crew efficiency, and distinct crew-set count. The current prototype does not implement retiming or the complete four-term objective.

## Output interpretation

- `output/joint_recovery/{1W,1M,154D}/summary.json`: solver status, counts, blockers, and validation summary.
- `output/joint_recovery/horizon_summary.json`: cross-horizon comparison.
- `output/one_week_joint_recovery/`: detailed one-week joint prototype artifacts.
- `output/recovery/`: readiness/export packages only; these are not optimized schedules.
- `originals/`: supplied source files, kept intact.
- `input/`: prepared runtime inputs.
- `src/crew_recovery/`: reusable project logic.
- `scripts/`: runnable audits and experiments.
- `tests/`: automated regression tests.
- `docs/`: technical decisions, runbooks, and reports.

No source input should be moved or overwritten merely to make the directory tree look smaller. Historical logs and `patches/*.orig` files are evidence/archive material, not active solver modules.
