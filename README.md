# Aircraft Crew Recovery

A prototype for crew-pairing generation, legality checks, and seeded disruption experiments. It is **not** a production aircraft-and-crew recovery optimizer: the available selection model optimizes flight coverage and pairing count only.

## Canonical layout

- `originals/`: immutable source data and supplied implementation files.
- `input/`: generated runtime inputs. Created by `setup_workspace.py`; do not edit or commit.
- Root Python modules: experiment orchestration, validation, rule adaptation, and audits.
- `tests/` and `tests/fixtures/`: regression tests and pinned historical baseline data.
- `logs/mip_1W.log`, `logs/mip_155D.log`: saved coverage-MIP results used by the objective-readiness audit.
- `archive/coverage_mip.py`: archived generic coverage model, retained for provenance; not an active solver.
- `reports/baseline/`: small historical audit snapshots, not live run output.
- `output/`: generated workbooks, logs, and machine-readable reports; intentionally untracked.

## Setup and test

```bash
python -m pip install -r requirements.txt
python setup_workspace.py
python -m pytest -q tests
```

`setup_workspace.py` verifies the canonical files in `originals/`, removes exact duplicate schedule rows (ignoring the export-only `Unnamed: 0` column), records duplicate rows under `output/audit/`, and regenerates `input/schedule.xlsx` and `input/pairing_report.xlsx`. Source artifacts are not overwritten.

## Main commands

```bash
# One-week disruption/pairing experiment
python run_pairing_experiment.py --horizons 1W

# Revenue join audit and objective-layer readiness
python revenue_audit.py
python objective_layer_readiness.py
```

The separate full-horizon entry point is `run_pairing_experiment_166_days.py`. It has a historical filename, but the source schedule spans **155 calendar days**, inclusive. With the current MGT workbook it correctly stops for arrivals beyond the last effective rule date rather than silently reusing expired rules. Obtain an authoritative rule update before treating that horizon as complete.

## Rule semantics and assumptions

`mgt_effective.py` filters supplied MGT rows by inclusive `Start Date`/`End Date` and by `Freq`, then delegates rule resolution to the unchanged source implementation in `originals/MGT.py`. The current adapter interprets weekday digits 1–7 as ISO weekdays and blank frequency as daily; the data owner must confirm this convention.

The pairing generator/experiment also uses explicitly labelled demo assumptions, including aircraft groups and the 72-hour layover cap. These are not official operating rules and must be confirmed before operational use.

## Optimizer boundary

The current coverage baseline minimizes uncovered-flight slack first, then the number of selected pairings. It does not model aircraft-tail trackability, crew roster feasibility, cancellation decisions, flight-copy/retiming variables, crew-efficiency variables, or distinct crew-set assignments. The four-term objective in `objective_spec.py` is an interface contract only; integration remains blocked until the actual production solver and its decision variables are supplied.

Revenue-derived cancellation costs remain disabled until the route-direction convention, fare meaning/units, abnormal load factors, and equipment-code crosswalk are confirmed. See `OBJECTIVE_LAYER_READINESS.md` for measured join coverage, baseline decomposition, and blockers.

## Historical snapshots

Files under `reports/baseline/` are snapshots of previous analyses, not automatically refreshed build products. Live outputs are written to `output/` and can be regenerated where the repository contains the corresponding workflow.
