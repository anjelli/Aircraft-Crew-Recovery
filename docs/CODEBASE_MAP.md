# Codebase map

This document distinguishes the canonical recovery path from older research and compatibility entry points. New solver code belongs under src/crew_recovery/, runnable commands belong under scripts/, tests belong under tests/, and project notes belong under docs/. Do not create duplicate implementations at the repository root.

## Canonical recovery execution

1. `setup_workspace.py` checks source inputs under `originals/` and invokes `scripts/prep_inputs.py`.
2. `scripts/prep_inputs.py` writes `input/schedule.xlsx` and `input/pairing_report.xlsx`; exact duplicate schedule rows are recorded under `output/audit/`.
3. `scripts/run_joint_recovery_horizons.py` invokes `scripts/run_one_week_joint_recovery.py` for each requested horizon and aggregates each run's summary.
4. The single-horizon runner prepares the schedule and resolves MGT, applies optional versioned scenario events, validates the supplied pairing report, optionally adds bounded generated candidates, loads aircraft resources, and runs operate/cancel recovery.
5. Each run checks aircraft routes, crew coverage, and schedule reconciliation. It exports per-flight decisions, selected pairings, aircraft rotations, validation reports, `schedule_reconciliation.csv`, an Excel workbook, and `summary.json`.
6. The multi-horizon runner carries each horizon's objective ledger and schedule-reconciliation summary into `horizon_summary.json`.
7. `scripts/audit_aircraft_line_baseline.py` independently audits the supplied AIRFLITE line plan. It is separate from validation of the newly recovered plan.

## Current architecture

```mermaid
flowchart TD
  subgraph INPUTS["Inputs and preparation"]
    S0["originals/schedule_kgp.xlsx"] --> PREP["scripts/prep_inputs.py"]
    P0["originals/pairing_report.xlsx"] --> PREP
    PREP --> SCH["input/schedule.xlsx"]
    PREP --> REP["input/pairing_report.xlsx"]
    RULES["originals/MGT.xlsx"]
    LINES["originals/low_kgp"]
  end

  subgraph ACTIVE["Active recovery pipeline"]
    H["Multi-horizon runner"] --> R["Single-horizon runner"]
    SCH --> R
    RULES --> R
    SCENARIO["Optional disruption scenario"] -.-> R
    R --> ACTIVE_FLIGHTS["Prepare schedule, apply scenario,<br/>resolve MGT, select eligible flights"]
    ACTIVE_FLIGHTS --> VALIDATE["Validate existing pairings"]
    REP --> VALIDATE
    VALIDATE --> EXISTING["Existing validated candidates"]
    EXISTING -.-> PRICER["Optional bounded candidate generation"]
    EXISTING --> POOL["Candidate pool"]
    PRICER -.-> POOL

    LINES --> RESOURCES["Load aircraft resources"]
    SCH --> RESOURCES
    INVENTORY["Optional verified aircraft inventory"] --> RESOURCES
    SCENARIO -.-> RESOURCES
    ACTIVE_FLIGHTS --> SOLVE["Recovery selection<br/>1W: joint MILP<br/>1M / 154D: heuristic"]
    POOL --> SOLVE
    RESOURCES --> SOLVE
    SOLVE --> CHECKS["Aircraft, crew, and schedule-reconciliation checks"]
    CHECKS --> OUTPUTS["Per-horizon exports and summary"]
    OUTPUTS --> AGG["Aggregate horizon_summary.json"]
  end

  subgraph AUDIT["Independent baseline audit"]
    BASE_AUDIT["audit_aircraft_line_baseline.py"]
    AUDIT_OUT["Baseline line-audit report"]
    SCH --> BASE_AUDIT
    RULES --> BASE_AUDIT
    LINES --> BASE_AUDIT
    BASE_AUDIT --> AUDIT_OUT
  end

  subgraph RESEARCH["Separate coverage-LP research path"]
    BUILDER["Pairing builder utilities"]
    ADAPTER["pairing_columns.py"]
    CG["column_generation.py +<br/>integrated_column_generation.py"]
    BUILDER -.-> ADAPTER
    ADAPTER -.-> CG
  end
```

The coverage-LP/master-pricing loop is deliberately shown as a separate research path. The canonical horizon runner does not invoke it, and it does not receive dual prices from the joint aircraft-and-crew recovery model.

The one-week horizon uses the joint MILP; larger horizons use the deterministic coverage-first heuristic. The current objective minimizes cancellations first and then uses the supported pairing and aircraft-start cost terms. Retiming is not optimized. When approved aircraft inventory is absent, AIRFLITE `LINE_n` resources remain provisional. The schedule-reconciliation audit checks output consistency; it is not a complete operational-feasibility certificate.

## Reusable implementation: src/crew_recovery/

| Module | Purpose |
|---|---|
| aircraft_inventory.py | Loads and validates explicit aircraft resource inputs when supplied. |
| aircraft_line_audit.py | Reconciles line assignments, station/time transitions, and MGT completeness. |
| aircraft_line_inputs.py | Reads the supplied AIRFLITE lines-of-work input. |
| aircraft_rotation.py | Route/rotation feasibility helpers. |
| crew_costing.py | Computes the currently supported pairing salary basis. |
| independent_validator.py | Active runner's MGTResolver, schedule preparation, and independent crew-pairing legality checks. |
| mgt_effective.py | Separate effective-date MGT resolver utility; not the resolver entry point imported by the active single-horizon runner. |
| objective_spec.py | Legacy four-term objective API retained for compatibility. |
| operational_objective.py | Canonical five-term objective contract and ledger; distinguishes active, partial-proxy, and unintegrated terms. |
| recovery_scenario.py | Strict versioned disruption-event schema and application to flight schedules / aircraft resources. |
| schedule_reconciliation.py | Compares original, scenario-adjusted, and recovered times/equipment; checks output consistency without claiming full operational feasibility. |
| pairing_columns.py | Converts supplied/generated pairing reports to stable candidate columns and validation status. |
| integrated_column_generation.py | Restricted master/pricing research loop; certification is limited to the declared pricing domain. |
| resource_constrained_pricing.py | Enumerates/prices pairings in its bounded duty domain. |
| joint_recovery.py | Joint operate/cancel selection and large-horizon heuristic plan comparison. |
| project_paths.py | Resolves canonical files from originals/ and prepared inputs from input/. |

## Runnable commands: scripts/

| Script | Purpose |
|---|---|
| setup_workspace.py / prep_inputs.py | Validate source presence and create canonical prepared input files. |
| run_joint_recovery_horizons.py | Canonical 1W / 1M / 154D run. |
| run_one_week_joint_recovery.py | Per-horizon operate/cancel selection, post-solve checks, and schedule reconciliation export. |
| audit_aircraft_line_baseline.py | Reconciles original line assignments with the prepared schedule and MGT. |
| audit_full_horizon_readiness.py / audit_recovery_gap.py | Additional readiness and gap audits. |
| run_pairing_experiment.py / run_pairing_experiment_166_days.py | Historical disruption/pairing experiments; not the canonical recovery entry point. |
| revenue_audit.py | Schedule-to-revenue join quality audit; it does not guess a revenue/equipment mapping. |
| objective_layer_readiness.py | Inspects saved coverage-only baselines and reports why the complete objective remains blocked. |

## Tests and automation

The tests/ folder covers pairing legality, MGT effective dates, aircraft resource/route behavior, objective contracts, recovery selection, independent audit logic, pricing, schedule reconciliation, aggregate horizon reporting, and historical experiment helpers. The GitHub workflow runs the full suite before producing the recovery artifact.

## Canonical files and legacy files

- originals/ is the sole source of truth for provided data/code inputs; identical root-level copies of supplied workbooks/CSVs have been removed.
- input/ contains prepared runtime copies and can be regenerated.
- output/ holds historical committed experiments; the current recovery results are produced under the ignored output/joint_recovery/ path and attached to the CI run as an artifact.
- logs/ retains historical logs for the objective-readiness analysis.
- patches/ contains archived .orig snapshots only; they are not imported as active solver code.
- Root-level launchers and import shims are compatibility surfaces. Add features in scripts/ or src/crew_recovery/, not in duplicate root modules.
- COLUMN_GENERATION_FOUNDATION.md and OBJECTIVE_LAYER_READINESS.md now live under docs/.

## Model boundary

The repository is a tested best-effort crew-pairing and aircraft-line recovery prototype plus an LP-pricing research layer. It is not a production operational crew-recovery optimizer: it does not select retimed flight copies, use verified tail registrations when no approved inventory is provided, implement the complete approved operating-cost/crew-set objective, or prove global optimality for the large-horizon heuristic.
