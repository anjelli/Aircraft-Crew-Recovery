# Codebase Map

This document explains the repository's current layout and execution flow. New solver tests and research notes should extend the existing `tests/` and `docs/` locations instead of creating additional top-level files or duplicate implementation modules.

## Execution flow

1. `scripts/setup_workspace.py` checks source artifacts under `originals/` and invokes `scripts/prep_inputs.py`.
2. `scripts/prep_inputs.py` prepares runtime inputs under `input/`; duplicate schedule rows are recorded under `output/audit/`.
3. A script under `scripts/` runs a disruption experiment or audit.
4. Experiments use `src/crew_recovery/mgt_effective.py`, supplied code under `originals/`, and paths defined in `src/crew_recovery/project_paths.py`.
5. `column_generation.py` and modules under `src/crew_recovery/` provide the isolated LP research baseline and pricing prototype.
6. `pytest` under `tests/` covers validation, MGT effective dates, objective readiness, revenue joins, LP mechanics, and independent pricing checks.

## Reusable implementation: `src/crew_recovery/`

| File | Purpose |
|---|---|
| `__init__.py` | Marks the reusable package. |
| `independent_validator.py` | Independently checks pairing legality without importing the supplied builder or MGT implementation. |
| `mgt_effective.py` | Filters MGT rules by inclusive effective dates and ISO weekday frequency before delegating to supplied MGT logic. |
| `objective_spec.py` | Defines the four-component objective contract; it is not connected to a production optimisation solver. |
| `pairing_columns.py` | Converts builder output into stable, independently validated LP columns and preserves multi-day metadata. |
| `integrated_column_generation.py` | Solves the restricted master, calls a pricing oracle, admits improving columns, and distinguishes certified from inconclusive stopping. |
| `resource_constrained_pricing.py` | Enumerates and prices the declared single-duty and two-duty pairing domain. Exactness is limited to that domain. |
| `project_paths.py` | Resolves canonical source files from `originals/` and prepared files from `input/`. |

## Runnable scripts: `scripts/`

| File | Purpose |
|---|---|
| `setup_workspace.py` | Checks required source artifacts and invokes input preparation. |
| `prep_inputs.py` | Prepares canonical runtime workbooks and audits exact duplicate schedule rows. |
| `run_pairing_experiment.py` | Runs week/month disruption experiments, validates existing pairings, generates candidates, and optionally runs column-generation benchmarks/pricing. |
| `run_pairing_experiment_166_days.py` | Historical full-horizon runner name; the committed schedule currently spans 155 calendar days. It does not perform an integrated crew-assignment MIP stage. |
| `revenue_audit.py` | Audits schedule-to-revenue joins, equipment matches, unmatched rows, and anomalies; it does not infer an equipment crosswalk. |
| `objective_layer_readiness.py` | Reports whether a production solver exists and decomposes saved coverage-only objective logs. |

## Tests and automation

| File | Purpose |
|---|---|
| `tests/conftest.py` | Makes repository modules importable during test collection. |
| `tests/test_core.py` | Core validator, experiment helper, disruption reproducibility, and baseline checks. |
| `tests/test_mgt_effective.py` | MGT effective-date/frequency, strict path resolution, and fail-closed behavior. |
| `tests/test_objective_layer_readiness.py` | Calendar-day span and coverage-objective decomposition tests. |
| `tests/test_objective_spec.py` | Objective priority/weight validation and gating for unimplemented components. |
| `tests/test_revenue_audit.py` | Revenue join, duplicate-key, and anomaly-screen tests. |
| `tests/test_column_generation.py` | Restricted-master LP, finite-pool pricing, and direct-LP comparisons. |
| `tests/test_resource_constrained_pricing.py` | Targeted overnight, insufficient-rest, and integrated pricing-loop tests. |
| `tests/test_resource_constrained_pricing_exhaustive.py` | Independent brute-force reference enumeration and reduced-cost/certification checks on tiny schedules. |
| `.github/workflows/tests.yml` | Prepares runtime inputs, runs audits, and executes the complete pytest suite on PRs and pushes to `main`. |

## Source-of-truth and historical files

- `originals/` contains supplied source code and input artifacts; do not overwrite these during refactoring.
- `patches/` contains historical `.orig` snapshots, not active modules.
- `input/` contains prepared runtime inputs.
- `output/` contains generated workbooks, audits, and logs.
- Root-level Python wrappers preserve legacy import/command paths. Keep them until downstream usage is confirmed; do not add duplicate implementations at the root.

## Important boundary

The repository remains a crew-pairing generation/validation and disruption-experiment prototype with an LP pricing research layer. It is not yet a complete production crew-recovery solver with full crew-roster, aircraft-tail continuity, operational objective, and integer-optimality guarantees.
