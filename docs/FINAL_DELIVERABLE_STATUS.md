# Recovery handoff: results and outstanding data

## Why the earlier horizons did not finish

The previous runner returned early if any flight in a horizon had missing/conflicting MGT. That meant a small number of unresolved rule lookups could prevent a useful schedule for the remaining flights. The one-month MILP also had thousands of flight, pairing and route-flow variables; at the earlier 30-second limit it sometimes returned no feasible incumbent. Merely increasing the limit is not a reliable solution for the full horizon.

A further reproducibility problem was that the joint runner read schedule, pairing report and rule files directly from the repository root, despite the project defining `originals/` as source-of-truth and `input/` as prepared runtime data. The runner now uses the prepared timetable/report and immutable original rule sources.

## New execution behavior

- One week uses the joint MILP on the small instance.
- One-month and 154-day instances use a deterministic feasible heuristic instead of waiting for a very large MILP to find its first incumbent.
- Flights with missing/conflicting/out-of-effective-date MGT are excluded from optimization and exported as `UNRESOLVED_MGT`. They are not silently assigned an invented turnaround time or counted as cancellations.
- The heuristic only accepts a crew pairing when all its flights can be inserted into compatible aircraft-line routes while maintaining starting station/time, equipment equality, station continuity and predecessor MGT.
- Every output reports solver mode and explicitly labels heuristic results as non-optimal.

## Run and locate results

```bash
python -m pip install -r requirements.txt
python setup_workspace.py
python -m pytest -q tests
python scripts/run_joint_recovery_horizons.py --time-limit-seconds 180
```

Results: `output/joint_recovery/1W/`, `1M/`, `154D/`, and `output/joint_recovery/horizon_summary.json`. Each horizon writes a workbook, flight decisions, selected pairings, aircraft rotations, validation files and `summary.json`.

This environment did not execute the GitHub Actions job, so the new numerical outputs are not yet verified. Use the uploaded `recovered-schedules` artifact from the PR workflow as the execution evidence; do not reuse historical logs as if they were newly computed results.

## What to ask the data owner for

1. **MGT exceptions:** corrected authoritative MGT rows for each flight in `missing_mgt_rules.csv` / `UNRESOLVED_MGT`, including equipment/subfleet, arrival station, effective start/end dates, frequency and carrier scope. Confirm conflicting matching rows rather than choosing one by row order.
2. **Aircraft starting state:** actual tail registration (or an explicit approval to use AIRFLITE line IDs as abstract resources), equipment/subfleet, initial station and availability timestamp.
3. **Equipment compatibility:** approved mapping for `320`, `32N`, `32P`, and `32Q`. Current model assumes exact-code compatibility.
4. **Objective confirmation:** approved definitions and priorities for cancellations, retiming, crew efficiency and distinct crew-set count. The current output does not optimize retiming or the complete four-term objective.

## Interpretation limits

- `OPERATE` means feasible under the current model and supplied candidate pool, not operational approval.
- `CANCEL` means not selected in the restricted solution. It is not proof of true operational infeasibility; the pairing report may not contain all legal pairings.
- `LINE_n` is a provisional line-of-work resource, not a verified tail registration.
- The long-horizon deterministic heuristic is feasible/non-optimal, not a global MILP optimum.
