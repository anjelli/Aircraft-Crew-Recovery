# Aircraft Crew Recovery

This repository contains a prototype crew-pairing validation and disruption experiment. The current MIP is a coverage baseline, not a complete operational recovery optimizer.

## Canonical input layout

- `originals/`: source-of-truth files supplied to the project. Do not overwrite them during preparation.
- `input/`: prepared runtime inputs consumed by the experiment scripts.
- `output/`: generated workbooks, audits, and logs.

Run `python setup_workspace.py` to build `input/schedule.xlsx` and `input/pairing_report.xlsx`. The schedule preparation removes exact duplicate rows using all columns except the export-only `Unnamed: 0` index column, and records duplicate rows under `output/audit/`. The pairing report is copied unchanged. Runtime code resolves source files only from `originals/` and prepared files only from `input/`; it does not silently select root-level or numbered filename variants.

## Code layout

- `src/crew_recovery/`: reusable validation, MGT effective-date handling, objective-contract, and path modules.
- `scripts/`: workspace setup, input preparation, disruption experiments, revenue audit, and objective-readiness report.
- Root-level Python names remain as small compatibility wrappers, so existing commands and imports still work.
- `tests/`: regression tests; `originals/`: supplied source-of-truth code and input artifacts; `patches/`: historical `.orig` snapshots, not active modules.
- `input/` and `output/` retain their existing roles; no schedule, rules, historical result, or output files are moved by this organization change.

See [`docs/CODEBASE_MAP.md`](docs/CODEBASE_MAP.md) for a file-by-file explanation and suggested execution flow.

## MGT effective dates

The supplied `MGT.py` handles rule specificity and arrival-time windows but does not filter rules by `Start Date`, `End Date`, or `Freq`. The experiment adapter therefore filters the rule table separately for each flight's local scheduled arrival date before calling the supplied implementation.

Date boundaries are inclusive. `Freq` is interpreted as ISO weekday digits (Monday=1, Sunday=7); a blank frequency is treated as all days. This frequency convention must be confirmed with the airline before operational use.

If no MGT rule applies to a flight, the experiment stops and reports the affected arrivals instead of applying an expired rule. The committed MGT source currently ends on 27 March 2027, while the schedule includes 28 March 2027, so the full-horizon experiment is expected to stop until that data gap is reconciled with the source owner. Do not extend or invent a rule to make the run pass.

The full-horizon script keeps its historical filename for compatibility, but defaults to the 155 calendar days present in the committed schedule. Set `HORIZON_DAYS` explicitly to override this. The output filenames also retain the historical `166_days` suffix for compatibility; the summary sheet reports the configured horizon. With the current MGT workbook, a stop on arrivals dated 28 March is expected; obtain an authoritative rule update before treating the horizon as complete.

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

## Revenue audit and objective readiness

Run the reproducible join audit, the saved baseline objective decomposition, and the regression tests:

```bash
python revenue_audit.py
python objective_layer_readiness.py
python -m pytest -q tests
```

The revenue audit writes join-rate, unmatched-key, anomaly, and per-equipment reports under `output/revenue_audit/`. The objective-readiness script writes the current generic-MIP objective decomposition and the four requested operational component statuses under `output/objective_readiness/`.

**Important:** repository inspection did not find the production column-generation/aircraft-timeline solver; `patches/mip.py.orig` is only an archived coverage MIP. The four-term operational objective is therefore exposed as a tested interface but is marked blocked/not run until the real solver and decision variables are supplied. Revenue cancellation cost remains disabled pending confirmation of fare units, PLF anomalies, route direction, and equipment-code mapping. See `OBJECTIVE_LAYER_READINESS.md`.

## Column-generation research foundation

An isolated LP research baseline lives in `column_generation.py`. It solves a restricted master, reads flight-coverage duals, prices unseen pairings by reduced cost, and records convergence. Its current pricing oracle scans an explicitly supplied finite candidate pool; this is a controlled validation step, not yet a resource-constrained shortest-path pricing algorithm. `src/crew_recovery/pairing_columns.py` adapts builder output into independently validated columns and preserves multi-day metadata.

Run the focused tests with:

```bash
python -m pytest -q tests/test_column_generation.py
```

To run the optional builder-to-column integration and compare finite-pool column generation against the full-pool LP on the actual schedule, use:

```bash
python run_pairing_experiment.py --horizons 1W --column-generation-benchmark

# Experimental exact pricing over the declared single-/two-duty pairing domain
python run_pairing_experiment.py --horizons 1W --resource-constrained-pricing
```

This optional run independently validates the builder's generated candidates and writes an audit report to `output/column_generation/1w_benchmark.json`. It may be expensive for the one-month horizon. The benchmark certifies results only over the supplied validated candidate pool, not over pairings the builder did not generate.

This module solves the **LP relaxation** for the supplied finite universe. It does not implement branch-and-price and cannot claim integer optimality. It must not be represented as the missing production recovery solver. The formulation and staged implementation gates are documented in `COLUMN_GENERATION_FOUNDATION.md`. The operational objective remains blocked until its decision variables and business assumptions are defined and validated.


## Joint recovery decision exports

The bounded joint operate/cancel runner can be used to generate decision-level CSV/workbook artifacts for the three requested horizons:

```bash
python scripts/run_joint_recovery_horizons.py --time-limit-seconds 30
```

It writes `output/joint_recovery/1W/`, `1M/`, and `154D/`, plus `output/joint_recovery/horizon_summary.json`. To keep longer runs bounded, this runner uses only independently validated existing pairing-report columns and skips candidate pricing. Therefore cancellations can reflect a limited pairing pool; they are not proof of operational infeasibility. Aircraft resources remain provisional `LINE_n` resources unless verified tail inventory is supplied. Retiming remains inactive in the current joint MILP. Always inspect each `summary.json`, including blockers, solver status, and independent validation, before using the outputs.

For the more expensive candidate-generation experiment, see the existing `run_one_week_joint_recovery.py` command and its `--skip-pricing` option. The readiness exports under `output/recovery/` are not optimized schedules.

See [Project Structure](docs/PROJECT_STRUCTURE.md) for the canonical folder layout and output interpretation.
