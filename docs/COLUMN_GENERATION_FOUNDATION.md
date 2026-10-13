# Column-generation foundation: scope and implementation contract

## Why this is a separate research layer

The active repository contains a pairing generator, an independent pairing validator, disruption experiments, and a coverage MIP archived at `../patches/mip.py.orig`. The archived MIP is not a production crew-recovery or aircraft-timeline optimizer. It selects candidate pairings to cover flights and penalizes uncovered flights:

\[
\min\;(N+1)\sum_f u_f + \sum_{p\in P}x_p,
\qquad
\sum_{p\in P} A_{fp}x_p + u_f = 1\quad\forall f.
\]

That formulation is a coverage baseline. It does not model individual crew rosters, aircraft-tail trackability, crew-set assignments, or the proposed operational four-term objective. The absence of those variables is a modeling boundary, not a missing code import.

## What counts as a column?

This repository currently has candidate **pairings**: feasible sequences of flights produced by `../pairings_builder.py`. A pairing column covers the flights in that sequence. It is not automatically a complete crew set or a crew roster across the recovery horizon.

For the first research milestone, pairing columns support a column-generation **LP relaxation baseline** for the coverage objective. The restricted master, dual values, reduced-cost arithmetic, and convergence must be validated separately from the pairing enumerator.

The eventual operational formulation must decide whether columns represent:
- a duty/pairing (appropriate only for the pairing-cover subproblem); or
- a complete feasible crew set / aircraft-compatible timeline (required if the master must enforce roster or tail trackability end-to-end).

Do not claim the second formulation is implemented until its source data and constraints are available.

## Current implementation

- `../column_generation.py`: restricted-master coverage LP, dual prices, reduced-cost calculation, and finite-pool baseline.
- `src/crew_recovery/integrated_column_generation.py`: loop that solves the restricted master, calls pricing, validates returned columns and distinguishes certified from inconclusive stopping.
- `src/crew_recovery/resource_constrained_pricing.py`: on-demand duty enumeration and pricing for single-duty hub turnarounds and two-duty hub–layover–hub pairings.
- `src/crew_recovery/independent_validator.py`: independent legality checks for completed candidates.
- `tests/test_resource_constrained_pricing.py`: targeted overnight, rest, and integrated-LP tests.
- `tests/test_resource_constrained_pricing_exhaustive.py`: brute-force reference enumerator for tiny schedules. It must not call the production pricer to construct its reference universe.

The pricing oracle is exact only over its declared bounded domain: at most two actual duties per pairing, the configured maximum sectors per duty, the supplied active schedule, and supplied layover stations. It does not price arbitrary multi-duty rosters, reserve/positioning decisions, or prove integer optimality.

## Validation gates

### Gate A — independent exhaustive pricing

On tiny schedules, enumerate flight permutations independently of the production pricer. Apply the documented connectivity, MGT, sector, FDP, layover, and independent-validator checks; compare legal pairing sequences and duty-split contracts against the production oracle.

The permanent test suite should include:
- multiple feasible connections and at least one disconnected/invalid alternative;
- a legal overnight pairing and an insufficient-rest rejection;
- negative, zero, and positive reduced-cost cases;
- the case where every improving candidate is already active;
- a check that convergence is certified only when no **unseen** improving column remains.

The brute-force reference is factorial and is intended only for tiny test instances. A matching candidate universe validates the bounded enumerator; it does not establish correctness for pairings outside the declared domain or resolve business-rule assumptions.

### Gate B — LP arithmetic

Compare column generation against a direct LP over the independently generated tiny-instance universe. Use hand-checkable reduced costs and multiple dual-price vectors. The LP objective and selected coverage/slack behavior should agree within numerical tolerance.

### Gate C — real-schedule audit

For the one-week experiment, report flight IDs with positive LP slack, candidate coverage by flight, and the selected LP variables. Reconcile candidate-union coverage with LP slack; they are different metrics and should not be conflated. Keep the one-week report reproducible.

### Gate D — scale-up

Only after Gates A–C pass, benchmark larger horizons. Record pricing runtime, restricted-master runtime, memory, candidate counts, iterations, best reduced cost, and the stop reason. Do not infer scalability from the pricing-oracle runtime alone because the experiment runner also executes the canonical builder.

## Integer recovery and operational objective

Column generation of the LP relaxation alone does **not** guarantee an optimal integer solution. An integer master over only columns generated at the root is a restricted-master incumbent, not a proof of integer optimality. Exact integer optimisation over a large implicit column set requires branch-and-price or another explicitly validated integer strategy.

The current `../objective_spec.py` intentionally disables revenue-derived cancellation cost and documents missing decision variables for retiming, crew efficiency, and distinct crew-set count. Keep these terms blocked until the required decisions, confirmed units, route/equipment joins, business priorities, and crew-set definitions are available. Do not reinterpret uncovered-flight slack as cancellation, pairing count as crew-set count, or arbitrary pairing cost as operational cost.

## Acceptance gates

- Existing baseline files and outputs remain reproducible.
- Every generated column passes independent legality checks and contains only active, in-horizon flight IDs.
- Tiny-instance exhaustive enumeration agrees with the production pricer on the declared domain.
- Tiny-instance column generation agrees with the direct LP over that independently enumerated universe.
- Logs report iterations, LP objective, columns added, best reduced cost, pricing status, and stop reason.
- LP optimality, integer optimality, and heuristic status are reported separately.
- No operational objective component is reported as evaluated unless its decision variables and validated coefficients are present.
