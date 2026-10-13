# Project structure

Keep one canonical runtime path. Preserve supplied source material and keep generated outputs out of the input path.

```text
Aircraft-Crew-Recovery/
├── originals/                 # Immutable supplied source files
├── input/                     # Prepared schedule.xlsx and pairing_report.xlsx
├── src/crew_recovery/         # Reusable MGT, validation, costing and solver code
├── scripts/
│   ├── prep_inputs.py
│   ├── setup_workspace.py
│   └── run_joint_recovery_horizons.py  # Canonical 1W / 1M / 154D entry point
├── tests/                     # Unit and regression tests
├── docs/                      # Architecture and handoff notes
├── output/joint_recovery/     # Current three-horizon deliverables
├── logs/                      # Historical run evidence only
└── patches/                   # Archived snapshots only; never imported as active code
```

Other existing audit and experiment scripts remain available for research, but they are not required for the standard recovery run. Root-level command wrappers are retained for compatibility; do not add a second implementation there.

## Canonical run

```bash
python -m pip install -r requirements.txt
python setup_workspace.py
python -m pytest -q tests
python scripts/run_joint_recovery_horizons.py --time-limit-seconds 180
```

## Input ownership

- Read prepared timetable and pairing report from `input/`.
- Read supplied MGT, FDP, cost and lines-of-work sources from `originals/`.
- Never choose an input based on whichever duplicate filename happens to exist in the repository root.
- Never edit or move the supplied files in `originals/`.
- The setup step logs exact duplicate schedule rows before preparing the canonical schedule.

## Output ownership

Each of `output/joint_recovery/{1W,1M,154D}/` contains a workbook, flight-level decisions, selected pairings, aircraft rotations, validation evidence and a JSON summary. `horizon_summary.json` compares horizons.

Interpret statuses precisely:
- `PROTOTYPE_OPTIMAL_WITH_PROVISIONAL_TAILS`: MILP optimal over the supplied pairing/route domain; line identifiers are provisional.
- `HEURISTIC_FEASIBLE_NOT_OPTIMAL`: deterministic large-horizon fallback found a constraint-feasible plan over supplied pairing columns.
- `PARTIAL_RECOVERY_UNRESOLVED_MGT`: a plan exists for flights with resolvable MGT; unresolved flights are explicitly listed and not counted as cancellations.
- Any `CANCEL` decision means excluded from this restricted plan, not proven impossible to operate with a richer candidate pool.

The long-horizon model is not a global optimizer. Retiming, verified physical tail registrations, and the full operational objective are not integrated.
