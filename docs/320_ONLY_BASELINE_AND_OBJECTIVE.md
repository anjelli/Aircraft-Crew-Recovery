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

## Objective priorities

Use a **lexicographic/staged solve**, not a single weighted sum with guessed large penalties. This is a proposed operational policy and requires business-owner confirmation.

1. **Hard feasibility:** crew legality, MGT, equipment compatibility, aircraft station continuity, crew rest/FDP and permitted retiming must hold.
2. **Minimize cancellations:** cancellation is the last resort; fix the minimum achievable cancellation count before lower-priority optimization.
3. **Minimize schedule impact:** among minimum-cancellation solutions, minimize total absolute retiming minutes; also report maximum delay and delay distribution.
4. **Minimize approved operating cost:** optimize crew costs, aircraft swapping/repositioning and unused duty/FDP costs only when cost definitions and units are understood.
5. **Tie-break on efficiency:** apply approved crew-set or other efficiency metrics only after higher-priority outcomes are fixed.

The supplied cost file has retiming tiers 0.001, 0.003 and 0.006 for 5/10/15-minute offsets, but no cancellation cost. These values must not be treated as directly comparable to a cancellation penalty. The revenue workbook also has incomplete joins and unresolved fare/PLF anomalies; it must not be used as a cancellation penalty until those issues are resolved.

## Implementation gates

1. Reconcile the complete 154-day schedule and all AIRFLITE lines across all four equipment codes.
2. Validate the existing crew legality and MGT rule resolution on representative cases.
3. Produce a one-week integrated crew-and-aircraft solve with explicit operate/retime/cancel decisions.
4. Compare tiny instances against an independent exhaustive enumerator.
5. Scale the same model to one month and then 154 days without resetting aircraft or crew state at window boundaries.
6. Publish actual decisions, objective values, independent validation, and unresolved exceptions. A readiness export with `UNRESOLVED` rows is not a recovered schedule.
