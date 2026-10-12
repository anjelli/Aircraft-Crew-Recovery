"""Binary recovery master for explicit operate/retime/cancel decisions.

Pairing columns refer to flight-option IDs, not base flight IDs. An option can
represent the original schedule (action='operate') or a concrete retimed copy
(action='retime', with a nonzero retime_minutes). A selected pairing activates
the options it contains; every base flight must be covered exactly once or
cancelled. This is an integer restricted-master model, not a full pairing
generator or aircraft-tail model.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Mapping, Sequence

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import lil_matrix

from column_generation import PairingColumn


@dataclass(frozen=True)
class FlightOption:
    option_id: str
    flight_id: str
    action: str = "operate"
    retime_minutes: int = 0
    action_cost: float = 0.0

    def __post_init__(self) -> None:
        if not self.option_id.strip() or not self.flight_id.strip():
            raise ValueError("option_id and flight_id must be non-empty")
        if self.action not in {"operate", "retime"}:
            raise ValueError("FlightOption.action must be 'operate' or 'retime'; cancellation is a separate decision")
        if self.action == "operate" and self.retime_minutes != 0:
            raise ValueError("operate options must have retime_minutes=0")
        if self.action == "retime" and self.retime_minutes == 0:
            raise ValueError("retime options must have a nonzero retime_minutes")
        if not isfinite(float(self.action_cost)) or self.action_cost < 0:
            raise ValueError("action_cost must be finite and nonnegative")


@dataclass(frozen=True)
class IntegerRecoveryResult:
    objective: float
    selected_pairing_ids: tuple[str, ...]
    flight_actions: Mapping[str, Mapping[str, object]]
    cancelled_flights: tuple[str, ...]
    solver_message: str


def solve_integer_recovery(
    active_flight_ids: Sequence[str],
    flight_options: Sequence[FlightOption],
    pairing_columns: Sequence[PairingColumn],
    *,
    cancellation_costs: Mapping[str, float],
    time_limit_seconds: float | None = None,
) -> IntegerRecoveryResult:
    """Solve a binary pairing-cover model with explicit retiming and cancellation.

    Every active flight is assigned to exactly one selected pairing option or
    cancelled. Pairing columns must already be generated and independently
    validated. The objective charges each selected pairing's cost plus each
    included option's action_cost, or the flight's cancellation cost.

    This model certifies optimality only over the supplied columns/options.
    """
    flights = [str(fid) for fid in active_flight_ids]
    if not flights or len(flights) != len(set(flights)) or any(not f.strip() for f in flights):
        raise ValueError("active_flight_ids must be unique, non-empty, and contain at least one flight")
    flight_set = set(flights)

    options = list(flight_options)
    option_by_id = {option.option_id: option for option in options}
    if len(option_by_id) != len(options):
        raise ValueError("flight option IDs must be unique")
    if set(option.flight_id for option in options) - flight_set:
        raise ValueError("flight options reference unknown base flights")

    missing_costs = flight_set - set(cancellation_costs)
    if missing_costs:
        raise ValueError(f"missing cancellation costs for flights: {sorted(missing_costs)}")
    costs = {f: float(cancellation_costs[f]) for f in flights}
    if any(not isfinite(c) or c < 0 for c in costs.values()):
        raise ValueError("cancellation costs must be finite and nonnegative")

    columns = list(pairing_columns)
    column_ids = [c.column_id for c in columns]
    if len(column_ids) != len(set(column_ids)):
        raise ValueError("pairing column IDs must be unique")
    for column in columns:
        unknown = set(column.flight_ids) - set(option_by_id)
        if unknown:
            raise ValueError(f"pairing {column.column_id!r} references unknown flight options: {sorted(unknown)}")
        covered_flights = [option_by_id[oid].flight_id for oid in column.flight_ids]
        if len(covered_flights) != len(set(covered_flights)):
            raise ValueError(f"pairing {column.column_id!r} contains multiple options for the same base flight")

    n_columns, n_flights = len(columns), len(flights)
    n_vars = n_columns + n_flights
    if n_vars == 0:
        raise ValueError("recovery model has no decision variables")

    objective = np.zeros(n_vars, dtype=float)
    for j, column in enumerate(columns):
        objective[j] = float(column.cost) + sum(option_by_id[oid].action_cost for oid in column.flight_ids)
    for i, fid in enumerate(flights):
        objective[n_columns + i] = costs[fid]

    # One equality per base flight: selected pairing coverage + cancellation = 1.
    matrix = lil_matrix((n_flights, n_vars), dtype=float)
    for j, column in enumerate(columns):
        for option_id in column.flight_ids:
            flight_idx = flights.index(option_by_id[option_id].flight_id)
            matrix[flight_idx, j] = 1.0
    for i in range(n_flights):
        matrix[i, n_columns + i] = 1.0

    integrality = np.ones(n_vars, dtype=np.int32)
    bounds = Bounds(np.zeros(n_vars), np.ones(n_vars))
    options_for_solver = {"disp": False}
    if time_limit_seconds is not None:
        if not isfinite(float(time_limit_seconds)) or time_limit_seconds <= 0:
            raise ValueError("time_limit_seconds must be finite and positive")
        options_for_solver["time_limit"] = float(time_limit_seconds)

    result = milp(
        c=objective,
        integrality=integrality,
        bounds=bounds,
        constraints=LinearConstraint(matrix.tocsr(), np.ones(n_flights), np.ones(n_flights)),
        options=options_for_solver,
    )
    if result.x is None or result.status not in {0, 1}:
        raise RuntimeError(f"Integer recovery solve failed: status={result.status}; {result.message}")
    if result.status == 1 and result.x is None:
        raise RuntimeError(f"Integer recovery stopped without an incumbent: {result.message}")

    selected_idx = [j for j in range(n_columns) if result.x[j] > 0.5]
    cancelled = [flights[i] for i in range(n_flights) if result.x[n_columns + i] > 0.5]
    actions: dict[str, Mapping[str, object]] = {
        fid: {"action": "cancel", "retime_minutes": 0, "pairing_id": None}
        for fid in cancelled
    }
    for j in selected_idx:
        column = columns[j]
        for option_id in column.flight_ids:
            option = option_by_id[option_id]
            actions[option.flight_id] = {
                "action": option.action,
                "retime_minutes": int(option.retime_minutes),
                "pairing_id": column.column_id,
                "option_id": option.option_id,
            }

    if set(actions) != flight_set:
        raise RuntimeError("solver result did not produce exactly one recovery action for every active flight")
    return IntegerRecoveryResult(
        objective=float(result.fun),
        selected_pairing_ids=tuple(columns[j].column_id for j in selected_idx),
        flight_actions=actions,
        cancelled_flights=tuple(cancelled),
        solver_message=str(result.message),
    )
