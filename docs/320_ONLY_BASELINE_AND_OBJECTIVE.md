# Full-fleet recovery scope and objective policy

## Scope

The operational recovery horizon is **25 October 2026 through 27 March 2027 inclusive (154 departure dates)**. The canonical schedule contains four equipment codes: `320`, `32N`, `32P`, and `32Q`. All four remain in scope.

The supplied AIRFLITE lines-of-work export has a metadata row before the CSV header. Its line inventory is organized as follows:

| Line numbers | Equipment code | Intended use |
|---|---|---|
| 1–5 | 320 | Full-fleet inventory and recovery |
| 6–13 | 32N | Full-fleet inventory and recovery |
| 14–16 | 32P | Full-fleet inventory and recovery |
| 17–31 | 32Q | Full-fleet inventory and recovery |

The generalized `load_aircraft_line_inventory` loader reads all lines by default, reconciles the flight instances against the canonical schedule, and fails closed when flight IDs, equipment, routes, or the selected horizon do not reconcile. A 320-only wrapper remains solely for focused tests and debugging; it is not the project scope.

## Aircraft line interpretation and limitations

The loader labels resources `LINE_n` based on AIRFLITE line numbers. These are **provisional planning resources, not verified aircraft tail registrations**. Initial station and availability are inferred from each line's first departure within the selected horizon. This does not establish actual tail registration, maintenance state, pre-horizon location, or availability.

No cross-equipment compatibility is inferred. Default compatibility remains exact equipment-code equality until a validated crosswalk and owner-approved policy are available. A line that changes equipment code within the horizon is rejected instead of silently treated as a single compatible resource.

The full-fleet loader's behavior is covered by synthetic tests. **Production-input reconciliation across all four equipment types remains a run-time acceptance gate**; passing synthetic tests is not evidence that the actual full fleet has reconciled or that a recovery solve exists. `originals/` remains untouched.

## Objective priorities and current implementation

Use a **lexicographic/staged solve**, not a single weighted sum with guessed large penalties. This is a proposed operational policy and requires business-owner confirmation.

1. **Hard feasibility:** independently validate crew legality, MGT, equipment compatibility, aircraft station continuity, rest/FDP and any selected flight-copy timing.
2. **Minimize cancellations:** cancellation is the last resort; fix the minimum achievable cancellation count before lower-priority optimization.
3. **Minimize retiming:** among minimum-cancellation solutions, minimize total absolute retiming minutes, then report maximum delay and delay distribution. **Not yet integrated:** the current joint prototype does not generate/select retimed flight copies.
4. **Minimize approved operating cost:** crew salary, allowances, aircraft swapping/repositioning and unused duty/FDP costs only when cost definitions and units are understood. The current prototype now uses the supplied `man_day_cost` as salary per duty calendar day; a duty crossing midnight charges both calendar dates. Other allowances and actual aircraft repositioning costs remain outside the current objective.
5. **Tie-break on efficiency:** use approved crew-set metrics only after higher-priority outcomes are fixed. The current candidate-ordering heuristic ranks by salary cost per covered sector and duty count, but it never removes candidates. This is not a trained ML model and does not yet optimize distinct rostered crew sets.

### Current solver objective

For the integrated one-week prototype the implemented sequence is: (a) minimize cancellation count; (b) fix that count and minimize pairing salary costs plus aircraft start costs. Retiming, revenue-weighted cancellation, crew-roster assignment, full allowances, and unused-FDP costs are not active terms. Do not describe this as the complete production objective.

The supplied cost file has retiming tiers 0.001, 0.003 and 0.006 for 5/10/15-minute offsets, but no cancellation cost. These values must not be treated as directly comparable to a cancellation penalty. The revenue workbook also has incomplete joins and unresolved fare/PLF anomalies; it must not be used as a cancellation penalty until those issues are resolved.

## Implementation gates

1. Reconcile the complete 154-day schedule and all AIRFLITE lines across all four equipment codes.
2. Validate the existing crew legality and MGT rule resolution on representative cases.
3. Produce a one-week integrated crew-and-aircraft solve with explicit operate/retime/cancel decisions.
4. Compare tiny instances against an independent exhaustive enumerator.
5. Scale the same model to one month and then 154 days without resetting aircraft or crew state at window boundaries.
6. Publish actual decisions, objective values, independent validation, and unresolved exceptions. A readiness export with `UNRESOLVED` rows is not a recovered schedule.


## Verified tail-number input

The AIRFLITE export's `A/C Own` field contains the airline/owner code (observed as `TR`), not an aircraft registration. The line numbers therefore cannot be relabeled as tail numbers. A verified inventory may be supplied as `aircraft_inventory.csv` using the schema in `aircraft_inventory.csv.template`:

- `tail_id`: actual registration/tail number from the fleet roster
- `equipment`: exact equipment/subfleet code
- `start_station` and `available_at`: verified initial state for the solve window
- `verified`: must be `true`
- `source`: provenance of the roster/state data

The loader rejects duplicate registrations, unverified rows, and `LINE_n` placeholders. Until a verified fleet roster and start-state extract are supplied, the model can only run with provisional line resources and must not be called an operational tail assignment.

## Connection ranking and online scoring

The current network builder retains every MGT-feasible aircraft connection. It orders candidate arcs using a transparent baseline score that prefers a direct return to the previous origin and then a shorter feasible ground gap. This implements the meeting's idea as **ranking, not hard pruning**: the reported 49-to-7 reduction is not assumed to be universally safe, because discarding a connection can remove a globally useful route.

The pairing pricer also accepts an optional candidate scoring callback. The baseline score is salary cost per covered sector, then duty count. Every legal generated column is retained regardless of score. A trained ML/string scorer is not yet fitted or integrated; before it influences pruning, evaluate it on one- or two-day enumerations against exhaustive feasibility/optimality checks and record coverage, solve time, objective gap, and any dropped optimal columns. Prefer ranking and branching guidance first; only add dominance pruning when its safety is proved.

## Flight-copy retiming network

The production design still needs explicit copies of each flight at permitted offsets (source cost tiers currently specify 0, 5, 10, and 15 minutes), with station-time nodes and arcs constrained by equipment, station continuity, and applicable MGT. Crew legality must be rechecked on the selected copies, including local-night FDP bands. A retimed outbound/return connection must be evaluated as a linked network decision, not as an isolated flight shift. This copy-selection layer is not part of the current operate/cancel MILP.
