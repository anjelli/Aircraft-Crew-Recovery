"""Finite-pool column-generation mechanics for the crew-pairing coverage LP.

This module is a research baseline, not the production crew-recovery solver.
The pricing oracle scans a supplied finite universe of pairings; it does not
yet solve a resource-constrained shortest-path problem. The LP relaxation may
be solved to optimality over that finite universe when pricing converges.
It does not establish integer optimality (branch-and-price is not implemented).
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Iterable, Mapping, Sequence

import numpy as np
from scipy.optimize import linprog
from scipy.sparse import csc_matrix, eye, hstack


@dataclass(frozen=True)
class PairingColumn:
    """A candidate pairing represented by the flight IDs it covers."""

    column_id: str
    flight_ids: tuple[str, ...]
    cost: float = 1.0

    def __post_init__(self) -> None:
        if not str(self.column_id).strip():
            raise ValueError("column_id must be non-empty")
        if not self.flight_ids:
            raise ValueError(f"Column {self.column_id!r} covers no flights")
        if len(set(self.flight_ids)) != len(self.flight_ids):
            raise ValueError(f"Column {self.column_id!r} repeats a flight ID")
        if not isfinite(float(self.cost)) or float(self.cost) < 0:
            raise ValueError(f"Column {self.column_id!r} has invalid cost {self.cost!r}")


@dataclass(frozen=True)
class RestrictedMasterSolution:
    objective: float
    column_values: Mapping[str, float]
    uncovered_values: Mapping[str, float]
    dual_prices: Mapping[str, float]
    solver_message: str


@dataclass(frozen=True)
class ColumnGenerationResult:
    solution: RestrictedMasterSolution
    columns: tuple[PairingColumn, ...]
    converged: bool
    iterations: int
    best_reduced_cost: float | None
    history: tuple[Mapping[str, float | int | str], ...]
    stop_reason: str


def _validate_problem(
    active_flights: Sequence[str],
    columns: Sequence[PairingColumn],
    uncovered_penalty: float,
) -> tuple[list[str], dict[str, int]]:
    flights = [str(f) for f in active_flights]
    if not flights:
        raise ValueError("active_flights must not be empty")
    if len(flights) != len(set(flights)):
        raise ValueError("active_flights contains duplicate IDs")
    if not isfinite(float(uncovered_penalty)) or float(uncovered_penalty) <= 0:
        raise ValueError("uncovered_penalty must be finite and positive")

    index = {fid: i for i, fid in enumerate(flights)}
    seen_ids: set[str] = set()
    for column in columns:
        if column.column_id in seen_ids:
            raise ValueError(f"Duplicate column_id: {column.column_id!r}")
        seen_ids.add(column.column_id)
        unknown = sorted(set(column.flight_ids) - set(index))
        if unknown:
            raise ValueError(
                f"Column {column.column_id!r} contains inactive/unknown flights: {unknown[:10]}"
            )
    return flights, index


def solve_restricted_master(
    active_flights: Sequence[str],
    columns: Sequence[PairingColumn],
    uncovered_penalty: float,
) -> RestrictedMasterSolution:
    """Solve the continuous restricted-master LP.

    For each flight f, sum of selected pairing coverage plus uncovered slack
    equals one. Column variables and slack are continuous and nonnegative.
    Since each column covers at least one flight, the equalities imply each
    variable is at most one. Dual prices are the equality-row marginals.
    """
    flights, flight_index = _validate_problem(
        active_flights, columns, uncovered_penalty
    )
    n_flights, n_columns = len(flights), len(columns)

    row_idx: list[int] = []
    col_idx: list[int] = []
    data: list[float] = []
    for j, column in enumerate(columns):
        for fid in column.flight_ids:
            row_idx.append(flight_index[fid])
            col_idx.append(j)
            data.append(1.0)

    incidence = csc_matrix(
        (np.asarray(data, dtype=float),
         (np.asarray(row_idx, dtype=int), np.asarray(col_idx, dtype=int))),
        shape=(n_flights, n_columns),
        dtype=float,
    )
    # Slack variables keep the restricted master feasible from iteration zero.
    matrix = hstack(
        [incidence, eye(n_flights, format="csc", dtype=float)],
        format="csc",
    )
    objective = np.concatenate(
        [
            np.asarray([float(c.cost) for c in columns], dtype=float),
            np.full(n_flights, float(uncovered_penalty), dtype=float),
        ]
    )
    result = linprog(
        c=objective,
        A_eq=matrix,
        b_eq=np.ones(n_flights, dtype=float),
        bounds=(0.0, None),
        method="highs",
    )
    if not result.success or result.x is None:
        raise RuntimeError(
            f"Restricted master LP failed: status={result.status}; {result.message}"
        )

    col_values = {
        columns[j].column_id: float(result.x[j])
        for j in range(n_columns)
    }
    slack = result.x[n_columns:]
    uncovered_values = {
        fid: float(slack[i]) for i, fid in enumerate(flights)
    }
    dual_values = np.asarray(result.eqlin.marginals, dtype=float)
    dual_prices = {
        fid: float(dual_values[i]) for i, fid in enumerate(flights)
    }
    return RestrictedMasterSolution(
        objective=float(result.fun),
        column_values=col_values,
        uncovered_values=uncovered_values,
        dual_prices=dual_prices,
        solver_message=str(result.message),
    )


def reduced_cost(column: PairingColumn, dual_prices: Mapping[str, float]) -> float:
    """Reduced cost for a pairing column under flight-coverage dual prices."""
    missing = sorted(set(column.flight_ids) - set(dual_prices))
    if missing:
        raise ValueError(
            f"Dual prices missing for flights in {column.column_id!r}: {missing[:10]}"
        )
    return float(column.cost) - sum(float(dual_prices[f]) for f in column.flight_ids)


def finite_pool_price(
    all_columns: Sequence[PairingColumn],
    active_column_ids: set[str],
    dual_prices: Mapping[str, float],
    *,
    reduced_cost_tolerance: float = 1e-7,
    batch_size: int = 100,
) -> list[tuple[PairingColumn, float]]:
    """Return most-negative reduced-cost columns from a finite candidate pool.

    This is an exact pricing scan only with respect to the supplied pool. It
    does not discover pairings outside that pool.
    """
    if not isfinite(float(reduced_cost_tolerance)) or reduced_cost_tolerance < 0:
        raise ValueError("reduced_cost_tolerance must be finite and nonnegative")
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")

    scored: list[tuple[PairingColumn, float]] = []
    for column in all_columns:
        if column.column_id in active_column_ids:
            continue
        rc = reduced_cost(column, dual_prices)
        if rc < -float(reduced_cost_tolerance):
            scored.append((column, rc))
    scored.sort(key=lambda item: (item[1], item[0].column_id))
    return scored[:batch_size]


def run_finite_pool_column_generation(
    active_flights: Sequence[str],
    candidate_universe: Sequence[PairingColumn],
    *,
    initial_columns: Sequence[PairingColumn] = (),
    uncovered_penalty: float | None = None,
    reduced_cost_tolerance: float = 1e-7,
    batch_size: int = 100,
    max_iterations: int = 100,
) -> ColumnGenerationResult:
    """Run LP column generation against a fixed, explicitly supplied universe.

    The default penalty matches the repository's coverage-first baseline
    (number of active flights + 1) only when every candidate column has unit
    cost. For non-unit costs, callers must provide an approved penalty.
    """
    flights = [str(f) for f in active_flights]
    universe = list(candidate_universe)
    if max_iterations < 1:
        raise ValueError("max_iterations must be >= 1")
    if not universe and not initial_columns:
        # An empty universe is valid: the all-slack solution is optimal.
        pass
    universe_ids = [c.column_id for c in universe]
    if len(universe_ids) != len(set(universe_ids)):
        raise ValueError("candidate_universe contains duplicate column IDs")

    if uncovered_penalty is None:
        if any(abs(float(c.cost) - 1.0) > 1e-12 for c in universe):
            raise ValueError(
                "Provide uncovered_penalty explicitly when candidate costs are not all 1"
            )
        uncovered_penalty = float(len(flights) + 1)

    _validate_problem(flights, universe, uncovered_penalty)
    _validate_problem(flights, list(initial_columns), uncovered_penalty)
    universe_by_id = {c.column_id: c for c in universe}
    active: list[PairingColumn] = []
    for column in initial_columns:
        if column.column_id not in universe_by_id:
            raise ValueError(
                f"Initial column {column.column_id!r} is not in candidate_universe"
            )
        active.append(universe_by_id[column.column_id])
    active_ids = {c.column_id for c in active}
    if len(active_ids) != len(active):
        raise ValueError("initial_columns contains duplicate column IDs")

    history: list[Mapping[str, float | int | str]] = []
    final_solution: RestrictedMasterSolution | None = None
    final_best_rc: float | None = None
    converged = False
    stop_reason = "iteration_limit"

    for iteration in range(1, max_iterations + 1):
        solution = solve_restricted_master(
            flights, active, float(uncovered_penalty)
        )
        priced = finite_pool_price(
            universe,
            active_ids,
            solution.dual_prices,
            reduced_cost_tolerance=reduced_cost_tolerance,
            batch_size=batch_size,
        )
        # Find the global best reduced cost for accurate diagnostics, not only
        # the best member of a possibly truncated batch.
        unseen_rc = [
            reduced_cost(c, solution.dual_prices)
            for c in universe if c.column_id not in active_ids
        ]
        best_rc = min(unseen_rc) if unseen_rc else None
        final_solution, final_best_rc = solution, best_rc
        history.append({
            "iteration": iteration,
            "active_columns": len(active),
            "lp_objective": solution.objective,
            "best_unseen_reduced_cost": (
                float(best_rc) if best_rc is not None else 0.0
            ),
            "negative_columns_available": len([
                rc for rc in unseen_rc if rc < -reduced_cost_tolerance
            ]),
        })

        if not priced:
            converged = True
            stop_reason = "no_negative_reduced_cost_columns"
            break
        if iteration == max_iterations:
            stop_reason = "iteration_limit_with_improving_columns"
            break

        for column, _ in priced:
            active.append(column)
            active_ids.add(column.column_id)

    assert final_solution is not None
    return ColumnGenerationResult(
        solution=final_solution,
        columns=tuple(active),
        converged=converged,
        iterations=len(history),
        best_reduced_cost=final_best_rc,
        history=tuple(history),
        stop_reason=stop_reason,
    )
