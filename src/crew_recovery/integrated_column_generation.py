"""Integrated restricted-master/pricing loop for crew-pairing column generation.

The solver does not own or materialize a complete candidate universe. Each
iteration solves the restricted master, passes dual prices to a pricing oracle,
and adds new columns returned by that oracle. The oracle must build legal
columns from schedule and rule data.

This solves the LP relaxation of the coverage formulation only. It is not
branch-and-price and does not model aircraft-tail timelines or complete crew
rosters. Convergence is claimed only when pricing certifies no improving
column exists in its full pricing domain.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Callable, Mapping, Sequence

from column_generation import (
    PairingColumn,
    RestrictedMasterSolution,
    reduced_cost,
    solve_restricted_master,
)


@dataclass(frozen=True)
class PricingBatch:
    """One dynamic pricing-oracle response.

    columns must contain newly generated feasible columns only.
    best_reduced_cost is the best reduced cost found over the searched domain.
    certified_no_improving_column may be True only for exhaustive/certified
    pricing over the full domain.
    """

    columns: tuple[PairingColumn, ...]
    best_reduced_cost: float | None
    certified_no_improving_column: bool
    explored_states: int = 0
    status: str = "ok"


PricingOracle = Callable[
    [Mapping[str, float], frozenset[str], float, int], PricingBatch
]


@dataclass(frozen=True)
class IntegratedCGResult:
    solution: RestrictedMasterSolution
    columns: tuple[PairingColumn, ...]
    converged: bool
    iterations: int
    history: tuple[Mapping[str, float | int | str], ...]
    stop_reason: str
    pricing_status: str


def solve_with_pricing(
    active_flights: Sequence[str],
    pricing_oracle: PricingOracle,
    *,
    initial_columns: Sequence[PairingColumn] = (),
    uncovered_penalty: float | None = None,
    reduced_cost_tolerance: float = 1e-7,
    batch_size: int = 50,
    max_iterations: int = 100,
) -> IntegratedCGResult:
    """Solve the restricted master and invoke dynamic pricing in one loop.

    The caller supplies only seed columns and an oracle, not the complete
    column universe. The oracle receives current flight duals, active column
    IDs, reduced-cost tolerance, and requested batch size.

    Heuristic pricing may add improving columns, but cannot claim convergence
    simply because a call returned no columns.
    """
    flights = [str(f) for f in active_flights]
    if not flights or len(flights) != len(set(flights)):
        raise ValueError("active_flights must be non-empty and unique")
    if max_iterations < 1:
        raise ValueError("max_iterations must be >= 1")
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    if not isfinite(float(reduced_cost_tolerance)) or reduced_cost_tolerance < 0:
        raise ValueError("reduced_cost_tolerance must be finite and nonnegative")

    if uncovered_penalty is None:
        if any(abs(float(c.cost) - 1.0) > 1e-12 for c in initial_columns):
            raise ValueError(
                "Provide uncovered_penalty explicitly when seed costs are not all 1"
            )
        uncovered_penalty = float(len(flights) + 1)
    if not isfinite(float(uncovered_penalty)) or uncovered_penalty <= 0:
        raise ValueError("uncovered_penalty must be finite and positive")

    flight_set = set(flights)
    active = list(initial_columns)
    active_ids: set[str] = set()
    for column in active:
        if column.column_id in active_ids:
            raise ValueError(f"Duplicate seed column ID: {column.column_id!r}")
        unknown = set(column.flight_ids) - flight_set
        if unknown:
            raise ValueError(
                f"Seed column {column.column_id!r} covers unknown flights: {sorted(unknown)}"
            )
        active_ids.add(column.column_id)

    history: list[Mapping[str, float | int | str]] = []
    solution: RestrictedMasterSolution | None = None
    converged = False
    stop_reason = "iteration_limit"
    pricing_status = "not_run"

    for iteration in range(1, max_iterations + 1):
        solution = solve_restricted_master(flights, active, float(uncovered_penalty))
        batch = pricing_oracle(
            solution.dual_prices,
            frozenset(active_ids),
            float(reduced_cost_tolerance),
            int(batch_size),
        )
        pricing_status = str(batch.status)
        if batch.explored_states < 0:
            raise ValueError("pricing oracle explored_states cannot be negative")

        new_columns: list[PairingColumn] = []
        batch_ids: set[str] = set()
        for column in batch.columns:
            if column.column_id in active_ids or column.column_id in batch_ids:
                raise ValueError(f"Pricing oracle returned duplicate column ID: {column.column_id!r}")
            unknown = set(column.flight_ids) - flight_set
            if unknown:
                raise ValueError(
                    f"Priced column {column.column_id!r} covers unknown/inactive flights: {sorted(unknown)}"
                )
            rc = reduced_cost(column, solution.dual_prices)
            if rc >= -float(reduced_cost_tolerance):
                raise ValueError(
                    f"Pricing oracle returned non-improving column {column.column_id!r} "
                    f"with reduced cost {rc:.8g}"
                )
            new_columns.append(column)
            batch_ids.add(column.column_id)

        if batch.best_reduced_cost is not None and not isfinite(float(batch.best_reduced_cost)):
            raise ValueError("pricing oracle best_reduced_cost must be finite or None")
        if (
            batch.best_reduced_cost is not None
            and batch.best_reduced_cost < -reduced_cost_tolerance
            and not new_columns
        ):
            raise ValueError(
                "Pricing oracle reported a negative best reduced cost but returned no improving column"
            )

        history.append({
            "iteration": iteration,
            "active_columns": len(active),
            "lp_objective": solution.objective,
            "columns_added": len(new_columns),
            "best_reduced_cost": (
                float(batch.best_reduced_cost) if batch.best_reduced_cost is not None else 0.0
            ),
            "explored_states": int(batch.explored_states),
            "pricing_status": pricing_status,
            "certified": int(batch.certified_no_improving_column),
        })

        if new_columns:
            if iteration == max_iterations:
                stop_reason = "iteration_limit_with_improving_columns"
                active.extend(new_columns)
                active_ids.update(batch_ids)
                break
            active.extend(new_columns)
            active_ids.update(batch_ids)
            continue

        if batch.certified_no_improving_column:
            converged = True
            stop_reason = "pricing_certified_no_improving_column"
        else:
            stop_reason = "pricing_inconclusive_no_column_returned"
        break

    if solution is None:
        raise RuntimeError("Column generation terminated without solving the master")
    if stop_reason == "iteration_limit_with_improving_columns":
        solution = solve_restricted_master(flights, active, float(uncovered_penalty))

    return IntegratedCGResult(
        solution=solution,
        columns=tuple(active),
        converged=converged,
        iterations=len(history),
        history=tuple(history),
        stop_reason=stop_reason,
        pricing_status=pricing_status,
    )
