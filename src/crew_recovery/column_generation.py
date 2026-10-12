"""LP column-generation core for crew-pairing coverage.

This module deliberately owns the restricted master and pricing loop in one place.
A pricing oracle is injected so it can reuse the existing pairing builder and its
multi-day legality checks instead of reimplementing airline rules here.

Scope: LP relaxation of a flight-covering pairing model. This is not a complete
crew roster, aircraft recovery model, or branch-and-price implementation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Sequence

import numpy as np
from scipy.optimize import linprog


@dataclass(frozen=True)
class PairingColumn:
    """A validated pairing column; flight IDs must be unique within the column."""

    pairing_id: str
    flight_ids: tuple[str, ...]
    cost: float = 1.0
    is_multi_day: bool = False

    def __post_init__(self) -> None:
        if not self.pairing_id:
            raise ValueError("pairing_id must be non-empty")
        if not self.flight_ids:
            raise ValueError(f"{self.pairing_id}: pairing must cover at least one flight")
        if len(set(self.flight_ids)) != len(self.flight_ids):
            raise ValueError(f"{self.pairing_id}: duplicate flight ID in pairing")
        if not np.isfinite(self.cost) or self.cost < 0:
            raise ValueError(f"{self.pairing_id}: cost must be finite and non-negative")


@dataclass(frozen=True)
class PricingResult:
    columns: tuple[PairingColumn, ...]
    exhaustive: bool = False
    note: str = ""


@dataclass(frozen=True)
class ColumnGenerationResult:
    status: str
    objective: float
    selected_weights: Mapping[str, float]
    uncovered_weights: Mapping[str, float]
    iterations: int
    columns_added: int
    converged_over_pricing_domain: bool
    pricing_notes: tuple[str, ...]


PricingOracle = Callable[[Mapping[str, float], frozenset[str]], PricingResult]


def _validate_columns(columns: Iterable[PairingColumn], flights: frozenset[str]) -> list[PairingColumn]:
    result: list[PairingColumn] = []
    seen: set[str] = set()
    for col in columns:
        if col.pairing_id in seen:
            raise ValueError(f"duplicate pairing_id: {col.pairing_id}")
        unknown = set(col.flight_ids) - flights
        if unknown:
            raise ValueError(f"{col.pairing_id}: unknown flights {sorted(unknown)}")
        seen.add(col.pairing_id)
        result.append(col)
    return result


def solve_column_generation(
    flight_ids: Sequence[str],
    initial_columns: Iterable[PairingColumn],
    pricing_oracle: PricingOracle,
    *,
    uncovered_penalty: float = 1_000_000.0,
    reduced_cost_tolerance: float = 1e-7,
    max_iterations: int = 100,
) -> ColumnGenerationResult:
    """Solve the coverage LP, adding negative-reduced-cost columns from an oracle.

    The master is min sum(cost[p] * x[p]) + penalty * sum(u[f]) subject to
    sum(p covers f, x[p]) + u[f] = 1. Variables are continuous and nonnegative.

    Convergence is certified only when the oracle says its pricing domain was
    searched exhaustively and returns no improving column. A heuristic no-column
    result stops the loop but is explicitly *not* reported as convergence.
    """
    flights = tuple(dict.fromkeys(str(f) for f in flight_ids))
    if not flights:
        raise ValueError("flight_ids must contain at least one flight")
    if len(flights) != len(flight_ids):
        raise ValueError("flight_ids must be unique")
    if not np.isfinite(uncovered_penalty) or uncovered_penalty <= 0:
        raise ValueError("uncovered_penalty must be finite and positive")
    if max_iterations < 1:
        raise ValueError("max_iterations must be >= 1")

    flight_set = frozenset(flights)
    columns = _validate_columns(initial_columns, flight_set)
    active_ids = {c.pairing_id for c in columns}
    notes: list[str] = []
    total_added = 0

    for iteration in range(1, max_iterations + 1):
        n_cols, n_flights = len(columns), len(flights)
        objective = np.array([c.cost for c in columns] + [uncovered_penalty] * n_flights, dtype=float)
        a_eq = np.zeros((n_flights, n_cols + n_flights), dtype=float)
        flight_pos = {fid: i for i, fid in enumerate(flights)}
        for j, col in enumerate(columns):
            for fid in col.flight_ids:
                a_eq[flight_pos[fid], j] = 1.0
        for i in range(n_flights):
            a_eq[i, n_cols + i] = 1.0

        lp = linprog(
            objective,
            A_eq=a_eq,
            b_eq=np.ones(n_flights),
            bounds=(0, None),
            method="highs",
        )
        if not lp.success:
            return ColumnGenerationResult(
                status=f"master_failed: {lp.message}",
                objective=float("inf"),
                selected_weights={},
                uncovered_weights={},
                iterations=iteration,
                columns_added=total_added,
                converged_over_pricing_domain=False,
                pricing_notes=tuple(notes),
            )

        duals = {fid: float(lp.eqlin.marginals[flight_pos[fid]]) for fid in flights}
        priced = pricing_oracle(duals, frozenset(active_ids))
        candidates = _validate_columns(priced.columns, flight_set)
        improving: list[PairingColumn] = []
        for col in candidates:
            if col.pairing_id in active_ids:
                continue
            reduced_cost = col.cost - sum(duals[fid] for fid in col.flight_ids)
            if reduced_cost < -reduced_cost_tolerance:
                improving.append(col)

        notes.append(priced.note)
        if not improving:
            selected = {c.pairing_id: float(lp.x[j]) for j, c in enumerate(columns) if lp.x[j] > 1e-9}
            uncovered = {
                fid: float(lp.x[n_cols + i])
                for i, fid in enumerate(flights)
                if lp.x[n_cols + i] > 1e-9
            }
            return ColumnGenerationResult(
                status="optimal_over_pricing_domain" if priced.exhaustive else "stopped_without_certificate",
                objective=float(lp.fun),
                selected_weights=selected,
                uncovered_weights=uncovered,
                iterations=iteration,
                columns_added=total_added,
                converged_over_pricing_domain=bool(priced.exhaustive),
                pricing_notes=tuple(notes),
            )

        columns.extend(improving)
        active_ids.update(c.pairing_id for c in improving)
        total_added += len(improving)

    # Re-solve is intentionally not claimed: the last iteration added columns,
    # so the last LP objective/weights would be stale. Return explicit status.
    return ColumnGenerationResult(
        status="max_iterations_reached",
        objective=float("inf"),
        selected_weights={},
        uncovered_weights={},
        iterations=max_iterations,
        columns_added=total_added,
        converged_over_pricing_domain=False,
        pricing_notes=tuple(notes),
    )
