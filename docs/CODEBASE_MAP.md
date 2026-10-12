# Codebase Map

This document describes the repository's current code layout. This change moves implementation files and keeps compatibility wrappers at their previous root paths; it does not change pairing-generation or validation algorithms.

## Execution flow

1. `scripts/setup_workspace.py` checks required artifacts under `originals/` and calls `scripts/prep_inputs.py`.
2. `scripts/prep_inputs.py` prepares the runtime schedule and pairing report under `input/`; duplicate schedule rows are recorded under `output/audit/`.
3. A script in `scripts/` runs a disruption experiment or an audit.
4. The experiments use the effective-date adapter in `src/crew_recovery/mgt_effective.py`, the supplied code under `originals/`, and the path contract in `src/crew_recovery/project_paths.py`.
5. Pytest tests under `tests/` exercise validation, MGT date filtering, the objective contract/readiness report, and revenue joins.

## Reusable implementation: `src/crew_recovery/`

| File | Purpose |
|---|---|
| `__init__.py` | Marks the reusable package. |
| `independent_validator.py` | Independently checks pairing legality using rule data and documented assumptions; it intentionally does not import the supplied pairing builder or MGT implementation. |
| `mgt_effective.py` | Filters MGT rules by inclusive effective dates and ISO weekday frequency for each local arrival date, then delegates rule matching to the supplied MGT implementation. |
| `objective_spec.py` | Defines and validates the requested four-component objective contract. It is an interface/readiness contract, not a connected production optimization solver. |
| `project_paths.py` | Resolves canonical source files from `originals/` and prepared files from `input/`, avoiding accidental fallback to stale root copies. |

## Runnable scripts: `scripts/`

| File | Purpose |
|---|---|
| `__init__.py` | Marks the runnable-scripts directory as a Python package. |
| `setup_workspace.py` | Checks the required source inventory and invokes input preparation. |
| `prep_inputs.py` | Reads the canonical schedule and pairing report from `originals/`, removes exact schedule duplicates using the configured key columns, writes prepared workbooks under `input/`, and records duplicate rows under `output/audit/`. |
| `run_pairing_experiment.py` | Runs the shorter seeded disruption experiment (week/month horizons), validates existing pairings, generates candidate pairings, and reports coverage and runtime. |
| `run_pairing_experiment_166_days.py` | Runs the configured full-source-horizon disruption experiment; the filename is historical and the committed schedule currently spans 155 calendar days. It generates and validates pairing candidates but does not perform an integrated crew-assignment MIP stage. |
| `revenue_audit.py` | Audits schedule-to-revenue keys, exact equipment matches, unmatched rows, and anomaly screens; it does not infer an equipment crosswalk. |
| `objective_layer_readiness.py` | Reports whether a production solver implementation is present and decomposes saved coverage-only MIP logs into objective components. |

## Supplied code and archived snapshots

| File | Purpose |
|---|---|
| `originals/MGT.py` | Supplied MGT duration/rule-specificity implementation; the effective-date adapter filters rules before calling it. |
| `originals/pairings_builder.py` | Supplied pairing candidate builder and its legality helpers. |
| `patches/mip.py.orig` | Archived generic coverage-only MIP snapshot; not the production aircraft-timeline/column-generation solver. |
| `patches/run_pairing_experiment.py.orig` | Historical snapshot of the shorter experiment runner. |
| `patches/run_pairing_experiment_166_days.py.orig` | Historical snapshot of the full-horizon experiment runner. |

The remaining files under `originals/` are source input artifacts (workbooks, CSVs, and the schedule export), not Python modules. The root-level `MGT.py` and `pairings_builder.py` files forward to the supplied code under `originals/`.

## Compatibility wrappers

The root-level wrappers below preserve existing commands and imports while keeping implementations grouped by role.

| Root file | Forwards to |
|---|---|
| `MGT.py` | `originals/MGT.py` |
| `pairings_builder.py` | `originals/pairings_builder.py` |
| `independent_validator.py` | `src/crew_recovery/independent_validator.py` |
| `mgt_effective.py` | `src/crew_recovery/mgt_effective.py` |
| `objective_spec.py` | `src/crew_recovery/objective_spec.py` |
| `project_paths.py` | `src/crew_recovery/project_paths.py` |
| `setup_workspace.py` | `scripts/setup_workspace.py` |
| `prep_inputs.py` | `scripts/prep_inputs.py` |
| `run_pairing_experiment.py` | `scripts/run_pairing_experiment.py` |
| `run_pairing_experiment_166_days.py` | `scripts/run_pairing_experiment_166_days.py` |
| `revenue_audit.py` | `scripts/revenue_audit.py` |
| `objective_layer_readiness.py` | `scripts/objective_layer_readiness.py` |

## Tests and automation

| File | Purpose |
|---|---|
| `tests/conftest.py` | Makes repository modules importable during pytest collection. |
| `tests/test_core.py` | Exercises core validator behavior, experiment helpers, disruption reproducibility, and the available pinned baseline check. |
| `tests/test_mgt_effective.py` | Tests MGT effective-date/frequency filtering, strict path resolution, and fail-closed behavior when no current rule applies. |
| `tests/test_objective_layer_readiness.py` | Tests calendar-day span calculation and decomposition of a saved coverage-only objective. |
| `tests/test_objective_spec.py` | Tests objective priority/weight validation and gating requirements for cancellation, retiming, efficiency, and crew-set components. |
| `tests/test_revenue_audit.py` | Tests route normalization, exact join coverage, duplicate-key handling, and anomaly screening. |
| `.github/workflows/tests.yml` | GitHub Actions workflow that prepares the workspace, runs audits, and executes pytest on pull requests and pushes to `main`. |

## Important boundary

The repository remains a crew-pairing generation/validation and disruption-experiment prototype. The current archived coverage-only MIP is not a production recovery solver; folder organization does not add aircraft-tail recovery, column generation, crew assignment, or a complete operational objective.
