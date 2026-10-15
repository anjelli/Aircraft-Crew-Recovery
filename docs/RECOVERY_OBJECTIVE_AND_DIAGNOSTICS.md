# Recovery objective and unoperated-flight diagnostics

## Objective hierarchy and current objective ledger

The canonical proposed operational priority order is maintained in `src/crew_recovery/operational_objective.py`: (1) cancellation count, (2) approved absolute retiming, (3) validated incremental operating cost, (4) validated crew-efficiency cost, and (5) distinct crew-set count. This priority order is pending owner approval and is not fully integrated.

The current joint solver actually optimizes only two stages: minimize cancellations, then fix that count and minimize selected pairing cost plus aircraft-start cost. That secondary value is a partial cost proxy, not the full approved operating cost. Retiming, crew-efficiency cost and distinct crew-set count are not integrated; their values must be reported as null/not integrated, not zero. Each current recovery `summary.json` includes an `objective_ledger` with the active stages, component breakdown, reconciliation against the solver-reported secondary objective, and blocked terms. The four-term API in `objective_spec.py` is retained for legacy cost-helper compatibility and is not the active solver policy.

The long-horizon fallback cannot prove a global optimum. It compares the prior single-pass plans with chronological ordering, bounded retries for pairings rejected before prerequisite route legs were inserted, and (where the provisional AIRFLITE mapping matches the resources) line-preferred and line-locked plans. The summary reports coverage and the number of operated flights that remain on their original provisional line.

The pairing orders include:

- **Long columns first:** favor pairings covering more flights, then lower cost per covered flight.
- **Rare-flight columns first:** favor pairing columns that cover flights with fewer alternatives in the current candidate pool.
- **Cost per flight first:** favor lower salary per covered flight, with longer columns breaking ties.

The fallback chooses the plan with the most operated flights. Pairing plus aircraft-start cost breaks ties between plans with equal coverage. Each horizon's `summary.json` reports operated/cancelled counts for all three plans and the operated-flight gain against the previous long-column-first plan using the same candidate pool. Its result is a better heuristic baseline, not a proof of minimum possible cancellations.

## Constraints

The selection change does not deliberately relax safety or legality constraints. The following remain hard under the current model:

- Supplied pairing columns must be in the independently validated candidate pool.
- Each operated flight is covered by exactly one selected pairing.
- Aircraft equipment must match the model's compatibility mapping (exact code matching by default).
- Aircraft route insertion must respect start station/time, station continuity and the predecessor flight's MGT.
- Flights without an unambiguous applicable MGT rule remain `UNRESOLVED_MGT`; no MGT is fabricated.

Retiming is not implemented. There is no generated flight-copy selection or approved maximum retiming window, so current runs do not claim retimed recovery.

## Reason codes in `flight_decisions.csv`

| Code | Meaning |
|---|---|
| `NO_VALID_PAIRING_CANDIDATE` | No validated pairing in the candidate pool contains this flight. |
| `PAIRING_OVERLAP_WITH_SELECTED_PAIRING` | Every available pairing for this flight overlaps at least one flight selected by the chosen heuristic plan. |
| `NO_FEASIBLE_AIRCRAFT_ROUTE_FOR_AVAILABLE_PAIRING` | At least one non-overlapping candidate was available, but the heuristic could not insert it while satisfying the hard aircraft-route constraints. |
| `NOT_SELECTED_BY_JOINT_MILP` | A candidate exists, but the one-week MILP does not select the flight in its current candidate/aircraft model. This is not a global infeasibility proof. |
| `MGT_RULE_UNRESOLVED` | A data-quality blocker, not a cancellation: authoritative MGT was missing, conflicting, or outside its effective dates. |
| `NOT_CLASSIFIED_BY_HEURISTIC` | Fallback diagnostic safeguard. Inspect the candidate and aircraft feasibility reports; do not interpret as global infeasibility. |

Each eligible flight also reports `candidate_pairing_count`. The aggregate `summary.json` includes `unoperated_reason_counts_including_unresolved`.

These are explanations of the selected restricted plan. They are not certificates that a flight cannot be operated with a richer pairing pool, actual tail state, approved compatibility crosswalk, or retiming.

## Candidate pool modes

The horizon runner defaults to the bounded `existing-only` mode, using validated pairing-report columns. To try additional legal candidates within the project's declared duty domain, run one horizon at a time:

```bash
python scripts/run_joint_recovery_horizons.py --horizons 1W --pairing-mode expanded --output-root output/joint_recovery_expanded --time-limit-seconds 180
```

The generator validates candidates independently, but its declared domain is not guaranteed to contain every operationally legal pairing. Candidate generation can become expensive on month/full horizons; benchmark 1W first before expanding the horizon. The default full-horizon run remains bounded.

## Validation and remaining limits

Compare coverage, selected pairings, reason-code counts, independent crew/aircraft validation, runtime and cost against the previous baseline. A coverage increase is useful only if the independent validators still report no violations.

The model still lacks approved retiming, verified registered-tail starting states when `aircraft_inventory.csv` is absent, an owner-approved equipment compatibility crosswalk, some authoritative MGT rows, and the full cancellation/retiming/crew-efficiency/crew-set-count objective. These inputs must be resolved before describing the output as an operationally accepted schedule.
