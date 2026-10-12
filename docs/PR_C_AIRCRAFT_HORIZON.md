# PR C — Aircraft rotation and horizon integration status

## What this branch adds

- src/crew_recovery/aircraft_rotation.py: binary route-flow assignment for an explicit aircraft inventory. A tail has one initial station/time, flights require compatible equipment, and each tail's selected sequence must connect station-to-station with the preceding flight's minimum ground time.
- scripts/run_recovery_horizon.py: a deterministic direct-horizon runner for 1 week (7 calendar days), 1 month (31 calendar days), and the full 155-day schedule. It exports an Excel workbook and CSV/JSON files for each horizon.
- Tests for multi-leg aircraft state continuity, equipment mapping, horizon sizes, and blocked output semantics.
- CI runs all three horizon preflight exports and uploads them as artifacts.

## Important: this is not yet a recovered schedule

The horizon runner is fail-closed and currently emits BLOCKED_NOT_OPTIMIZED with every flight marked UNRESOLVED. This is intentional. It must not present the original schedule as a recovery result.

Known blockers from the source review:

1. Actual aircraft tail registrations and their initial station/availability states are not present in the supplied lines-of-work source. Line numbers are not tail registrations.
2. An authoritative aircraft/equipment compatibility crosswalk is missing. Equipment codes such as 32P and 32Q are not mapped by guesswork.
3. MGT rules have no effective coverage for at least 2027-03-28 in the full horizon; the authoritative rule owner must resolve this.
4. The crew pairing pricing prototype and integer restricted master are not yet integrated into one end-to-end model with aircraft route flow, retimed copies, cancellation decisions, and a shared objective.

The aircraft route-flow function is a standalone feasibility component, not yet coupled to crew pairings and recovery decisions. It requires the actual operated-flight set and authoritative MGT/equipment/tail inputs.

## Rolling windows

This runner does not use rolling windows. It runs each requested horizon directly. If rolling windows are introduced later, the ending tail station, availability time, equipment, and any maintenance/resource restrictions must be serialized as explicit state and supplied to the next window. No reset-to-origin or implicit carry-over is allowed.

## Run

    python -m pip install -r requirements.txt
    python setup_workspace.py
    python scripts/run_recovery_horizon.py --horizons 1W 1M 155D
    python -m pytest -q tests

Outputs are written under output/recovery/{1W,1M,155D}/. Each horizon folder contains recovery_<horizon>.xlsx, the flight-level plan, crew-pairing/aircraft-rotation/cancellation/retiming tables, validation results, unresolved exceptions, and recovery_summary.json.

## Acceptance criteria before calling the final schedule recovered

- Owner-approved tail inventory with initial states and maintenance constraints.
- Authoritative equipment compatibility and date-complete MGT rules.
- Integrated crew pairing and aircraft route-flow decisions.
- Explicit operate/cancel/retime decisions and approved costs.
- Tiny-instance exhaustive validation of the joint model.
- Feasible 1W, 1M, and 155D runs with independent validation and zero unexplained exceptions (or a clearly reported exception list).
