# Final deliverable status

**Handoff status: prototype deliverable ready for review; operational acceptance remains blocked.** This is a fixed scope. Do not extend the optimization scope for the handoff unless a reproducible defect is found or authoritative missing inputs are supplied.

## Reference execution

- Workflow: [GitHub Actions run 38733921684](https://github.com/anjelli/Aircraft-Crew-Recovery/actions/runs/38733921684)
- Tested PR head: b1ca91a6669d57e1646cf91e9f7e3352f3bbbd8e
- Result: success; the regression test suite, supplied-line baseline audit, all three horizon runs, and artifact upload passed.
- Execution mode: existing-only. Additional pairing generation was skipped for bounded runtime; generated pairing candidates = 0.
- Output package: [download recovered schedules and audit evidence](https://github.com/anjelli/Aircraft-Crew-Recovery/actions/runs/38733921684/artifacts/11972375014).

This pinned workflow run is the source of truth for the results below. A separate 1-week JSON supplied during the discussion reports 582 operated flights and 591 validated candidates; that is not the same result snapshot as this workflow, which reports 570 and 352. The available evidence does not establish why those runs differ. Do not mix metrics from the separate run into this table.

## Recovery results

| Horizon | Method/status | Active flights | Eligible flights | Operated | Cancelled | Unresolved MGT | Coverage | Validated existing candidates | Selected pairings | Aircraft violations / crew coverage errors |
|---|---|---:|---:|---:|---:|---:|---|---:|---:|---:|
| 1W · 25–31 Oct 2026 | Joint MILP; prototype optimum only within the restricted model | 825 | 825 | 570 | 255 | 0 | 69.09% | 352 | 249 | 0 / 0 |
| 1M · 25 Oct–24 Nov 2026 | Deterministic heuristic; not globally optimal | 3,763 | 3,763 | 2,286 | 1,477 | 0 | 60.75% | 1,629 | 990 | 0 / 0 |
| 154D · 25 Oct 2026–27 Mar 2027 | Deterministic heuristic; unresolved MGT remains | 19,231 | 19,222 | 12,302 | 6,920 | 9 | 63.97% of all; 64.00% of eligible | 8,433 | 5,327 | 0 / 0 |

Cancelled counts exclude the nine unresolved-MGT flights. They are reported separately. Post-solve checks reported zero aircraft-route violations and zero crew-coverage errors for the selected plans under the current provisional assumptions.


## Schedule reconciliation findings

The current run wrote `schedule_reconciliation.csv` for each horizon. All operated flights retained the scenario-adjusted times, and the audit reported zero internal consistency failures. This is a result-consistency check, not a certificate of operational feasibility.

| Horizon | Flights reconciled | Scenario-modified flights | Operated flights matching scenario times | Consistency failures |
|---|---:|---:|---:|---:|
| 1W | 825 | 0 | 570 / 570 | 0 |
| 1M | 3,763 | 0 | 2,286 / 2,286 | 0 |
| 154D | 19,231 | 0 | 12,302 / 12,302 | 0 |

All three results used the default `baseline_no_disruption` scenario, so the scenario-modified count is zero. Cancellations therefore were not triggered by explicit scenario events in this run; they reflect the outcome of the restricted candidate pool and the current recovery method. This does not establish that all those flights were operationally cancellable.


## What the heuristic comparison demonstrates

All plans in each long-horizon comparison use the same flight set, existing validated pairing candidate pool, and provisional aircraft inventory. Up to eight deterministic plans are evaluated. The selected plan is the one with the most operated flights; the supplied pairing plus aircraft-start cost breaks ties.

| Horizon | Prior single-pass operated | Selected retry plan operated | Gain | Coverage gain |
|---|---:|---:|---:|---:|
| 1M | 1,136 | 2,286 | +1,150 | +30.56 percentage points |
| 154D | 6,544 | 12,302 | +5,758 | +29.96 percentage points |

The selected long-horizon strategy is free chronological retry, with two passes reported in each summary. Line-preferred and line-locked plans are diagnostic alternatives, not physical-tail assignments. For 154D, the line-locked chronological plan operated 10,346 flights versus 12,302 for the selected free-reassignment retry plan; the coverage-first rule therefore selected the latter. This comparison supports the conclusion that the old one-pass heuristic left flights unoperated that the retry heuristic could recover. It does not prove the selected plan is globally optimal.

The 1W MILP result has no comparable heuristic-plan table in its output. Its optimality flags apply only to the supplied validated pairing columns and generated provisional aircraft route arcs.

## Independent aircraft-line baseline audit

The audit reconciled the supplied AIRFLITE line assignments with the prepared schedule and MGT source:

- 19,231 assigned flights across 31 provisional lines.
- 19,200 consecutive line transitions checked.
- 0 station discontinuities, 0 time overlaps, and 0 known MGT shortfalls on checked transitions.
- 9 flights still have unresolved MGT.
- Overall audit status: UNRESOLVED_MGT_RULES; full baseline feasibility is **not** proven.

The audit result is included in the downloadable workflow artifact. LINE_n values are proxies inferred from lines of work, not verified aircraft registrations.

## What is in the artifact

The recovered-schedules artifact contains the three horizon folders, each with:
- summary.json, including the objective ledger and schedule-reconciliation KPIs;
- flight_decisions.csv and schedule_reconciliation.csv;
- selected_pairings.csv and aircraft_rotations.csv;
- pairing validation status and violation reports;
- aircraft validation output;
- a recovery workbook with a Schedule Reconciliation worksheet;
- missing_mgt_rules.csv for the 154-day horizon.

The audit folder includes the aircraft-line transition audit, its summary, data-quality/revenue audit outputs, and duplicate-row audit from workspace preparation. Run-specific outputs are not stored as current results in the Git tree; the artifact is the versioned execution evidence for this snapshot.

## Reproduce locally

Use Python 3.11 from the repository root:

    python -m pip install -r requirements.txt
    python setup_workspace.py
    python -m pytest -q tests
    python scripts/run_joint_recovery_horizons.py --time-limit-seconds 180

The default run uses existing-only validated pairing columns and skips candidate generation to keep runtime bounded. Expanded mode is experimental and must be benchmarked separately, beginning with 1W.

## Fixed scope and remaining limitations

The following remain outside the accepted implementation:

1. **Verified aircraft starting states:** no approved aircraft_inventory.csv is supplied. Actual tail registration/equipment, starting station, and availability time are not available. Current LINE_n assignments remain provisional.
2. **MGT source completion:** nine flights have missing/conflicting/out-of-effective-date MGT. They must remain unresolved until the data owner supplies authoritative rules.
3. **Approved compatibility:** exact equipment-code matching/demo mappings are not a substitute for an airline-approved equipment/subfleet crosswalk.
4. **Full objective:** the 1-week model minimizes cancellations, then supplied pairing salary plus aircraft-start cost, within its restricted model. Long-horizon heuristic results are not globally optimal. Full approved operating cost, retiming/flight-copy selection, crew roster/set variables, and globally complete pairing generation are not integrated.
5. **Owner acceptance:** operational constraints and final objective priorities require owner sign-off.

## Handoff decision

The tested prototype, reproducible entry point, three-horizon output package, reason-coded flight decisions, independent audits, and scope statement form the project handoff. Treat the work as complete at this prototype boundary. Do not describe it as operationally accepted or start another optimization round just to pursue a higher coverage percentage. Reopen the technical scope only for a concrete reproducible defect or when an authoritative input blocker is resolved.
