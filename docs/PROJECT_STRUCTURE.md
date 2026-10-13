# Project structure and ownership

This is the canonical layout for the repository. Preserve supplied source material and generated evidence separately.

\`\`\`text
Aircraft-Crew-Recovery/
├── originals/                 # Supplied source files and raw inputs; immutable
├── input/                     # Prepared canonical runtime inputs
├── src/crew_recovery/          # Reusable validation, costing, pricing, network and solver modules
├── scripts/                   # Executable experiments, audits and recovery runners
├── tests/                     # Unit, regression and exhaustive-reference tests
├── docs/                      # Architecture, objective policy, runbooks and project notes
├── patches/                   # Historical .orig snapshots only; never imported as active code
├── output/                    # Generated reports, workbooks, CSV/JSON and audit artifacts
├── logs/                      # Historical run logs; not authoritative current outputs
└── *.py                       # Backward-compatible command wrappers; keep until downstream use is retired
\`\`\`

## Where to put files

- **Never edit or move files from \`originals/\`.** Keep source inputs and supplied implementations byte-for-byte intact.
- Put cleaned/prepared copies in \`input/\`; the workspace setup script is the single preparation entry point.
- Put importable business logic in \`src/crew_recovery/\`. Do not create a second implementation at the repository root.
- Put runnable commands in \`scripts/\`; root-level Python files are compatibility wrappers for established command names.
- Put automated checks in \`tests/\` and operational assumptions / run instructions in \`docs/\`.
- Treat \`output/\` and \`logs/\` as generated or historical evidence. Do not hand-edit generated schedules and do not commit large run outputs as source code.
- Keep \`patches/*.orig\` as archive material only; they are not active solvers.

## Canonical commands

From the repository root:

\`\`\`bash
python -m pip install -r requirements.txt
python setup_workspace.py
python -m pytest -q tests
python scripts/run_recovery_horizon.py --horizons 1W 1M 154D
python scripts/run_one_week_joint_recovery.py --start-date 2026-10-25 --days 7 --time-limit-seconds 120
python scripts/run_joint_recovery_horizons.py --time-limit-seconds 30
\`\`\`

## Interpreting recovery output

- \`output/recovery/\` contains **readiness exports**, not optimizer decisions; \`UNRESOLVED\` means no decision was made.
- \`output/one_week_joint_recovery/\` contains the one-week joint prototype's decision-level artifacts.
- \`output/joint_recovery/{1W,1M,154D}/\` contains bounded operate/cancel runs using existing validated pairing-report columns only.
- Read each \`summary.json\` before using its CSVs. Check status, blockers, uncovered/cancelled counts, aircraft violations, crew coverage errors, tail-inventory flag and optimality scope.
- A run that is optimal over supplied pairing columns is not globally optimal over every possible legal pairing.
- \`LINE_n\` resources are provisional lines of work, not registered aircraft. Do not report them as actual tail assignments.
- Current joint runs do not optimize flight-copy retiming. The configured lexicographic policy lists retiming ahead of operating cost, but the retiming term remains inactive until flight-copy selection is integrated.
