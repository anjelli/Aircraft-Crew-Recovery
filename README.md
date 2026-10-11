# Aircraft Crew Recovery

This repository contains a prototype crew-pairing validation and disruption experiment. The current MIP is a coverage baseline, not a complete operational recovery optimizer.

## Canonical input layout

- `originals/`: source-of-truth files supplied to the project. Do not overwrite them during preparation.
- `input/`: prepared runtime inputs consumed by the experiment scripts.
- `output/`: generated workbooks, audits, and logs.

Run `python setup_workspace.py` to build `input/schedule.xlsx` and `input/pairing_report.xlsx`. The schedule preparation removes exact duplicate rows using all columns except the export-only `Unnamed: 0` index column, and records duplicate rows under `output/audit/`. The pairing report is copied unchanged. Runtime code resolves source files only from `originals/` and prepared files only from `input/`; it does not silently select root-level or numbered filename variants.

## MGT effective dates

The supplied `MGT.py` handles rule specificity and arrival-time windows but does not filter rules by `Start Date`, `End Date`, or `Freq`. The experiment adapter therefore filters the rule table separately for each flight's local scheduled arrival date before calling the supplied implementation.

Date boundaries are inclusive. `Freq` is interpreted as ISO weekday digits (Monday=1, Sunday=7); a blank frequency is treated as all days. This frequency convention must be confirmed with the airline before operational use.

If no MGT rule applies to a flight, the experiment stops and reports the affected arrivals instead of applying an expired rule. The committed MGT source currently ends on 27 March 2027, while the schedule includes 28 March 2027, so the full-horizon experiment is expected to stop until that data gap is reconciled with the source owner. Do not extend or invent a rule to make the run pass.

The full-horizon script retains a historical 166-day default, but the committed schedule contains 155 days. To target the available data horizon, run `HORIZON_DAYS=155 python run_pairing_experiment_166_days.py` (PowerShell: `$env:HORIZON_DAYS=155; python run_pairing_experiment_166_days.py`). With the current MGT workbook, a stop on arrivals dated 28 March is expected; obtain an authoritative rule update before treating the horizon as complete.

## Install and test

```bash
python -m pip install -r requirements.txt
python -m pytest -q tests
```

Focused MGT regression tests can be run with:

```bash
python -m pytest -q tests/test_mgt_effective.py
```

Run a one-week experiment with:

```bash
python run_pairing_experiment.py --horizons 1W
```

The original `MGT.py` and `pairings_builder.py` are not modified by the effective-date adapter.
