# 320-only baseline and recovery objective policy

## Aircraft line interpretation

The supplied AIRFLITE lines-of-work file has a metadata row before its CSV header. In the supplied file, lines 1–5 contain only equipment code `320`; lines 6–13 are `32N`, lines 14–16 are `32P`, and lines 17–31 are `32Q`. The initial experiment therefore uses lines 1–5 and exact `Eqp=320` matching. It does not assume interchangeability across subfleets.

Reconciliation for departure dates 25 October 2026 through 27 March 2027 found 3,807 scheduled `320` flights and 3,807 flights in lines 1–5. All matched exactly by flight number, departure timestamp, origin, destination and equipment.

| Provisional resource | Equipment | Initial station | Available from |
|---|---|---|---|
| LINE_1 | 320 | IPH | 25 Oct 2026 08:55 |
| LINE_2 | 320 | CGK | 25 Oct 2026 09:40 |
| LINE_3 | 320 | SIN | 25 Oct 2026 10:20 |
| LINE_4 | 320 | HAK | 25 Oct 2026 11:00 |
| LINE_5 | 320 | SIN | 25 Oct 2026 12:50 |

These are **provisional planning resources**, not actual aircraft tail numbers. The initial station and availability timestamp are inferred from each line's first listed flight. No claim is made about real tail registration, maintenance or availability before that time. The loader rechecks the source reconciliation and fails closed if the data changes. `originals/` remains untouched.

## Objective priorities

Use a **lexicographic/staged solve**, not a single weighted sum with guessed huge penalties. This is a defensible industry-style recovery policy, but remains a proposed policy until the airline approves it.

1. **Hard feasibility:** all crew legality, MGT, equipment, station continuity and permitted retiming constraints must hold.
2. **Minimize cancellations:** cancellation is the last resort. First find the minimum achievable cancellation count; fix that optimum before optimizing lower-priority objectives.
3. **Minimize schedule impact:** among minimum-cancellation solutions, minimize total absolute retiming minutes; also report maximum and distribution of delay.
4. **Minimize approved operating cost:** optimize crew costs, aircraft swapping/repositioning and unused duty/FDP costs only where cost definitions and units are understood.
5. **Tie-break on efficiency:** minimize crew-set count or other efficiency metrics after higher-priority outcomes are fixed.

The uploaded cost file has retiming costs 0.001, 0.003 and 0.006 for 5/10/15-minute tiers, but no cancellation cost. Combining these values directly could make a cancellation look cheaper than a retiming, contrary to the stated operational priority. Staged optimization avoids inventing a cancellation penalty. The file also contains `man_day_cost=785`, MGT assumptions and swapping-cost tiers; their provenance and objective units should be retained in reports rather than silently treated as fully approved.

## Scope and gates

- Begin with only the five provisional 320 resources through 27 March 2027.
- Keep other equipment codes out of this baseline.
- Independently validate crew and aircraft transitions and compare tiny-instance solutions against exhaustive enumeration.
- Expand to 32N/32P/32Q only after an explicit compatibility policy is established.
