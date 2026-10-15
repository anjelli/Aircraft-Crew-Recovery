# Aircraft Crew Recovery

A reproducible research prototype for crew-pairing and aircraft-line recovery across 1-week, 1-month, and 154-day horizons. It exports flight-level operate/cancel decisions, pairing and aircraft-route assignments, validation reports, and explicit unresolved-data findings.

**Status: tested prototype; not operationally accepted.** Aircraft resources are provisional LINE_n proxies, nine full-horizon flights have unresolved MGT, retiming is not optimized, and the complete approved business objective is not integrated.

## Run the prototype

Use Python 3.11 from the repository root:

    python -m pip install -r requirements.txt
    python setup_workspace.py
    python -m pytest -q tests
    python scripts/run_joint_recovery_horizons.py --time-limit-seconds 180

The canonical runner exports 1W (25–31 Oct 2026), 1M (25 Oct–24 Nov 2026), and 154D (25 Oct 2026–27 Mar 2027) to output/joint_recovery/. Each horizon summary includes an `objective_ledger` that separates the objective actually optimized from operational cost terms that remain unintegrated. Each run also writes `schedule_reconciliation.csv`, comparing original schedule values, scenario-adjusted values, and recovered decisions; this checks output consistency and does not certify full operational feasibility. Those run-specific outputs are regenerated locally and are not committed. The runner accepts an optional versioned disruption scenario; see [Scenario inputs](#scenario-inputs).

## Verified recovery snapshot

The run-pinned metrics, heuristic comparison, independent line audit, limitations, and artifact link are maintained in the [Final Deliverable Status](docs/FINAL_DELIVERABLE_STATUS.md). The successful workflow run is [38475474585](https://github.com/anjelli/Aircraft-Crew-Recovery/actions/runs/38475474585); it passed 108 tests and uploaded the three-horizon outputs and audit evidence.

## Scenario inputs

By default, the recovery runner uses `baseline_no_disruption`. To run a repeatable disruption experiment, copy `scenarios/template.json`, replace `scenario_id`, and add explicitly sourced events. Pass the same manifest to a one-week runner or the multi-horizon orchestrator:

```bash
python scripts/run_one_week_joint_recovery.py --days 7 --scenario-file scenarios/my_scenario.json --output-dir output/scenarios/my_scenario/1W
python scripts/run_joint_recovery_horizons.py --horizons 1W 1M --scenario-file scenarios/my_scenario.json --output-root output/scenarios/my_scenario
```

Supported schema version 1 events are:

- `FLIGHT_DELAY`: `event_id`, `flight_id`, positive integer `delay_minutes`. Shifts scheduled departure and arrival in both local and common-time columns.
- `FLIGHT_EQUIPMENT_CHANGE`: `event_id`, `flight_id`, `equipment_code`. The equipment code must already occur in the canonical schedule; this does not create a compatibility rule.
- `AIRCRAFT_UNAVAILABLE`: `event_id`, `aircraft_id`. Removes that resource for the entire run/window; time-bounded maintenance windows are not supported yet.

Use canonical `flight_id` values from the schedule or a prior output. Scenario edits preserve the original `flight_id` and service-date membership, record baseline and scenario times separately, and re-resolve MGT after a delay/equipment change. Each run writes `scenario_manifest.json` and includes applied/skipped events in its summary. Unknown flight/resource IDs and unsupported event types fail closed. The current optimizer still chooses operate/cancel decisions; applying a delay in a scenario is not the same as optimizing retiming.

## Candidate pool

The bounded default uses existing pairing-report columns that pass independent validation. To experiment with additional candidates in the declared pairing domain, begin with one week:

    python scripts/run_joint_recovery_horizons.py --horizons 1W --pairing-mode expanded --output-root output/joint_recovery_expanded --time-limit-seconds 180

Expanded generation can be expensive and is not guaranteed to enumerate every operationally legal pairing. Do not compare results from different pairing modes as if they were the same experiment.

## Interpreting decisions

- OPERATE: the flight is covered by a selected validated pairing and inserted into a feasible provisional aircraft route.
- CANCEL: the flight was not selected in the restricted candidate plan. This is not proof that cancellation is operationally unavoidable.
- UNRESOLVED_MGT: applicable turnaround data is missing, conflicting, or outside its effective dates. It is a data blocker, not a cancellation.
- A zero count in the post-solve aircraft and crew-coverage checks means no issue was found by those checks under the current assumptions; it does not certify a real-world aircraft assignment.
- `schedule_reconciliation.csv` distinguishes changes introduced by a disruption scenario from cancellations or unresolved flights in the recovery output. Since retiming is not optimized, operated-flight times must match the scenario-adjusted schedule.

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

Operational use requires authoritative effective-dated MGT for unresolved flights, verified aircraft tail registrations and starting states (or an explicit approval to use abstract line resources), an owner-approved equipment compatibility crosswalk, and owner confirmation of the full cost/retiming/crew-set objective. The current prototype must not be presented as an operationally approved airline schedule.
