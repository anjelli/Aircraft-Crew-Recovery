# Integrated column-generation implementation status

## What has changed on this branch

The module \`src/crew_recovery/integrated_column_generation.py\` owns the optimization loop:

1. Solve the restricted-master LP for the currently active pairing columns.
2. Pass flight-coverage dual prices and the active-column ID set to a pricing oracle.
3. Validate each returned column: it must be new, cover only active flights, and have negative reduced cost under the current duals.
4. Add improving columns to the same master and re-solve.
5. Claim LP convergence only if the pricing oracle explicitly certifies that no improving column exists over its full pricing domain. A heuristic timeout or empty heuristic response is inconclusive.

This moves the restricted-master/pricing control loop into one solver entry point rather than asking callers to run a master and a separate finite-pool scan manually. The existing \`column_generation.py\` remains as a finite-pool benchmark and mathematical reference.

## What is not yet integrated

The current repository does not contain the production aircraft-timeline/crew-recovery solver. In particular, \`patches/mip.py.orig\` is a coverage-only model. Therefore this branch does not claim to implement full operational crew recovery or branch-and-price.

The current module's pricing oracle is an interface, not yet a production pricing implementation. It must be connected to a dynamic resource-constrained path search that generates new legal duties from the disrupted schedule using the supplied builder's rules. That search must enforce, at minimum:

- airport connectivity and minimum ground time;
- local/UTC time consistency and retiming/disruption state;
- equipment continuity and equipment-change connection rules;
- duty-sector limits and applicable FDP limits;
- legal rest/layover transitions for multi-duty pairings;
- active-flight and recovery-horizon boundaries;
- independent validation of generated candidate duties.

The pricing state and objective must be reconciled with the actual master constraints before the resulting reduced costs are operationally meaningful. A flight-coverage dual alone is only correct for the coverage master currently implemented.

## Required next milestones

1. **Dynamic legal pricing:** implement the oracle against the schedule graph and validated rules; do not pre-enumerate the entire pairing universe.
2. **Operational master:** replace the coverage-only objective/constraints with the agreed recovery decisions and validated objective components once their units and semantics are confirmed. Do not interpret uncovered slack as cancellation or pairing count as crew-set count.
3. **Validation:** compare the dynamic pricing results against exhaustive generation on small instances; assert every generated column passes independent validation.
4. **Integer recovery:** use branch-and-price (including branching decisions respected by pricing) if integer optimality is required. An integer solve over root-generated columns alone is only a restricted-master incumbent.
5. **Learning-guided pricing:** first retain exact pricing as a baseline, then add ML ranking/partial pricing as an acceleration. If pricing is heuristic, report it as heuristic unless an exact/certified fallback establishes the bound.

## Current guarantee

The solver provides an integrated restricted-master + pricing loop and explicit convergence-status handling. It does **not** yet certify optimality over the real schedule because no schedule-backed dynamic pricing oracle is connected. It solves only the LP relaxation of the current coverage formulation; no integer or full operational recovery guarantee is made.
