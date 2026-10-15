# Aircraft Crew Recovery

A reproducible research prototype for crew-pairing and aircraft-line recovery across 1-week, 1-month, and 154-day horizons. It exports flight-level operate/cancel decisions, pairing and aircraft-route assignments, validation reports, and explicit unresolved-data findings.

**Status: prototype with a bounded disruption-retiming experiment; not operationally accepted.** The native joint solver still does not contain a flight-copy retiming objective, and the wrapper searches a finite grid around a small set of disrupted/nearby flights. Aircraft resources are provisional LINE_n proxies, nine full-horizon flights have unresolved MGT, and the complete approved business objective is not integrated.

## Run the prototype

Use Python 3.11 from the repository root:

    python -m pip install -r requirements.txt
    python setup_workspace.py
    python -m pytest -q tests
    python scripts/run_joint_recovery_horizons.py --time-limit-seconds 180

The canonical runner exports 1W (25–31 Oct 2026), 1M (25 Oct–24 Nov 2026), and 154D (25 Oct 2026–27 Mar 2027) to output/joint_recovery/. Each horizon summary includes an `objective_ledger` that separates the native solver's objective from operational cost terms that remain unintegrated. Each run also writes `schedule_reconciliation.csv`, comparing original schedule values, disruption-adjusted values, and recovered decisions; this checks output consistency and does not certify full operational feasibility. Those run-specific outputs are regenerated locally and are not committed. For an actual disruption experiment with bounded retiming selection, use the command below rather than an empty baseline scenario.

## Verified recovery snapshot

The run-pinned metrics, schedule reconciliation findings, heuristic comparison, independent line audit, and limitations are maintained in the [Final Deliverable Status](docs/FINAL_DELIVERABLE_STATUS.md). The current recovery snapshot and aggregate reconciliation outputs are available from [workflow run 38754773496](https://github.com/anjelli/Aircraft-Crew-Recovery/actions/runs/38754773496). The aggregate `horizon_summary.json` carries each horizon's objective ledger and reconciliation KPIs.

## Disruption recovery with bounded retiming search

First prepare the workspace, then run the checked-in non-empty 20-minute delay experiment on flight 485 (IPH–SIN). The search considers downstream pairing flights 486 and 487:

```powershell
python setup_workspace.py
python scripts/run_disruption_recovery.py --scenario-file scenarios/demo_disruption_1w.json --output-dir output/disruption_demo --max-retimed-flights 2 --retime-options=0,5,10,15 --time-limit-seconds 30 --require-positive-retiming --require-cancellation-improvement
```

The script applies the disruption, discovers downstream flights in the affected crew pairing, evaluates joint combinations of the configured offset grid, reruns pairing validation and the recovery solve for each combination, and keeps the best candidate using this order: cancelled flights, total absolute selected retiming among operated flights, then the native solver's partial pairing-plus-aircraft-start cost. It exports the selected run to `output/disruption_demo/recovered_schedule/`, including `flight_decisions.csv`, `aircraft_rotations.csv`, `selected_pairings.csv`, `schedule_reconciliation.csv`, `summary.json`, and `recovery_7d.xlsx`. `search_summary.json` records each trial and the chosen offsets.

This is a finite-grid outer-loop coordinate search, not a global joint MILP or continuous-time retiming proof. The inner solver still uses the existing validated pairing pool (or bounded generated columns in expanded mode); resource inventory and business objective limitations below still apply.

## Scenario inputs

By default, the recovery runner uses `baseline_no_disruption`. To run a repeatable disruption experiment, copy `scenarios/template.json`, replace `scenario_id`, and add explicitly sourced events. Pass a disruption-only manifest to `run_disruption_recovery.py` to search retiming choices, or use `run_one_week_joint_recovery.py` to apply a known offset exactly:

```bash
python scripts/run_one_week_joint_recovery.py --days 7 --scenario-file scenarios/my_scenario.json --output-dir output/scenarios/my_scenario/1W
python scripts/run_joint_recovery_horizons.py --horizons 1W 1M --scenario-file scenarios/my_scenario.json --output-root output/scenarios/my_scenario
```

Supported schema version 1 events are:

- `FLIGHT_DELAY`: `event_id`, `flight_id`, positive integer `delay_minutes`. Shifts scheduled departure and arrival in both local and common-time columns.
- `FLIGHT_EQUIPMENT_CHANGE`: `event_id`, `flight_id`, `equipment_code`. The equipment code must already occur in the canonical schedule; this does not create a compatibility rule.
- `AIRCRAFT_UNAVAILABLE`: `event_id`, `aircraft_id`. Removes that resource for the entire run/window; time-bounded maintenance windows are not supported yet.
- `FLIGHT_RETIME`: `event_id`, `flight_id`, signed `offset_minutes` (within ±180). Applies an explicit recovery offset after disruption events and before crew/aircraft feasibility checks. The recovered departure/arrival cannot precede the original source schedule. For automatic bounded selection rather than a manually supplied offset, use `scripts/run_disruption_recovery.py`.

Use canonical `flight_id` values from the schedule or a prior output. Scenario edits preserve the original `flight_id` and service-date membership, record baseline, disruption-adjusted, and recovered times separately, and re-resolve MGT after a delay/equipment change or nonzero retiming. Each run writes `scenario_manifest.json` and includes applied/skipped events in its summary. Unknown flight/resource IDs and unsupported event types fail closed. The native joint MILP still chooses operate/cancel decisions and its supported cost proxy; the bounded search wrapper compares separate solves for each retiming candidate and is not a proof of global optimality.

## Candidate pool

The bounded default uses existing pairing-report columns that pass independent validation. To experiment with additional candidates in the declared pairing domain, begin with one week:

    python scripts/run_joint_recovery_horizons.py --horizons 1W --pairing-mode expanded --output-root output/joint_recovery_expanded --time-limit-seconds 180

Expanded generation can be expensive and is not guaranteed to enumerate every operationally legal pairing. Do not compare results from different pairing modes as if they were the same experiment.

## Interpreting decisions

- OPERATE: the flight is covered by a selected validated pairing and inserted into a feasible provisional aircraft route.
- CANCEL: the flight was not selected in the restricted candidate plan. This is not proof that cancellation is operationally unavoidable.
- UNRESOLVED_MGT: applicable turnaround data is missing, conflicting, or outside its effective dates. It is a data blocker, not a cancellation.
- A zero count in the post-solve aircraft and crew-coverage checks means no issue was found by those checks under the current assumptions; it does not certify a real-world aircraft assignment.
- `schedule_reconciliation.csv` distinguishes original times, disruption-adjusted times, and recovered times. It checks that each operated flight matches its explicit chosen retiming offset; this is a consistency check, not a global retiming-optimality or full operational-feasibility certificate.

For long horizons, up to eight deterministic heuristic plans are compared. The chosen plan maximizes operated flights first and uses the supplied pairing/aircraft cost only to break coverage ties. This is a heuristic comparison, not a global optimality proof. The 1-week joint MILP proves its lexicographic optimum only over its restricted pairing and provisional-aircraft model.

## Source and folder ownership

- originals/: canonical, immutable supplied inputs and source implementations.
- input/: prepared schedule and pairing report generated by setup_workspace.py.
- src/crew_recovery/: reusable solver, MGT, costing, inventory, and validation modules.
- scripts/: canonical recovery runner plus preparation and audit commands.
- tests/: regression and small-instance tests.
- docs/: architecture, project map, objective contract, and handoff.
- output/: retained historical experiments plus ignored run-specific recovery outputs.
- logs/ and patches/: historical run evidence and archived snapshots; not active recovery implementations.

Redundant copies of the supplied workbooks and CSV files have been removed from the repository root. Root-level Python shims are retained for backward compatibility; add new implementation code under src/crew_recovery/ or scripts/, not as duplicate root modules.

See [Project Structure](docs/PROJECT_STRUCTURE.md), [Codebase Map](docs/CODEBASE_MAP.md), and [Recovery Objective and Diagnostics](docs/RECOVERY_OBJECTIVE_AND_DIAGNOSTICS.md).

## Operational blockers

Operational use requires authoritative effective-dated MGT for unresolved flights, verified aircraft tail registrations and starting states (or an explicit approval to use abstract line resources), an owner-approved equipment compatibility crosswalk, and owner confirmation of the full cost/retiming/crew-set objective. The bounded retiming wrapper is a finite-grid experiment, not integration of the full flight-copy retiming term into the native joint MILP. The current prototype must not be presented as an operationally approved airline schedule.
