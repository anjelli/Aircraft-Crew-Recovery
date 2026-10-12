"""Aircraft rotation assignment with explicit tail inventory and carried state.

This is an aircraft feasibility layer, not a crew pairing/recovery solver.
It assigns each supplied operated flight to one compatible aircraft and
prevents an aircraft from operating incompatible consecutive flight pairs.
No aircraft identity, station, equipment compatibility, or initial availability
is inferred from schedule line numbers.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from math import isfinite
from typing import Mapping, Sequence

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import lil_matrix


@dataclass(frozen=True)
class Aircraft:
    tail_id: str
    equipment: str
    start_station: str
    available_at: object
    start_cost: float = 0.0

    def __post_init__(self) -> None:
        if not self.tail_id.strip() or not self.equipment.strip() or not self.start_station.strip():
            raise ValueError("tail_id, equipment, and start_station must be non-empty")
        if not isfinite(float(self.start_cost)) or self.start_cost < 0:
            raise ValueError("start_cost must be finite and nonnegative")


@dataclass(frozen=True)
class AircraftRotationResult:
    assignments: Mapping[str, str]
    rotations: Mapping[str, tuple[str, ...]]
    objective: float
    solver_message: str


def _minutes(value: object) -> float:
    if value is None:
        raise ValueError("minimum ground time is missing")
    if hasattr(value, "total_seconds"):
        return float(value.total_seconds()) / 60.0
    parsed = float(value)
    if not isfinite(parsed) or parsed < 0:
        raise ValueError("minimum ground time must be finite and nonnegative")
    return parsed


def _flight_value(flight: Mapping[str, object], key: str) -> object:
    if key not in flight or flight[key] is None:
        raise ValueError(f"flight {flight.get('flight_id', '<unknown>')!r} is missing {key}")
    return flight[key]


def _can_follow(first: Mapping[str, object], second: Mapping[str, object]) -> bool:
    if str(first["To"]).strip().upper() != str(second["From"]).strip().upper():
        return False
    mgt = _minutes(first.get("mgt_min", first.get("MGT_td")))
    return second["STD"] >= first["STA"] + timedelta(minutes=mgt)


def solve_aircraft_rotations(
    flights: Sequence[Mapping[str, object]],
    aircraft: Sequence[Aircraft],
    *,
    compatible_equipment: Mapping[str, Sequence[str]] | None = None,
    time_limit_seconds: float | None = None,
) -> AircraftRotationResult:
    """Assign every operated flight to one aircraft, or fail with infeasibility.

    A tail can serve a flight only if the flight's equipment code is explicitly
    compatible and the aircraft is available at the departure station/time.
    Every pair of flights assigned to the same tail must form a feasible
    chronological connection. compatible_equipment is an explicit mapping
    from aircraft equipment code to permitted flight equipment codes; omitted
    entries default to exact code equality.
    """
    records = [dict(f) for f in flights]
    tails = list(aircraft)
    if not records:
        raise ValueError("flights must not be empty")
    if not tails:
        raise ValueError("aircraft inventory must not be empty")
    flight_ids = [str(_flight_value(f, "flight_id")) for f in records]
    if len(set(flight_ids)) != len(flight_ids):
        raise ValueError("flight_id values must be unique")
    tail_ids = [a.tail_id for a in tails]
    if len(set(tail_ids)) != len(tail_ids):
        raise ValueError("tail_id values must be unique")

    for f in records:
        for key in ("From", "To", "STD", "STA", "Eqp"):
            _flight_value(f, key)
        _minutes(f.get("mgt_min", f.get("MGT_td")))

    compatibility = {
        str(k).strip(): {str(v).strip() for v in values}
        for k, values in (compatible_equipment or {}).items()
    }
    allowed = np.zeros((len(tails), len(records)), dtype=bool)
    for ai, tail in enumerate(tails):
        accepted = compatibility.get(tail.equipment, {tail.equipment})
        for fi, flight in enumerate(records):
            equipment = str(flight["Eqp"]).strip()
            allowed[ai, fi] = (
                equipment in accepted
                and str(flight["From"]).strip().upper() == tail.start_station.strip().upper()
                and flight["STD"] >= tail.available_at
            )

    n_a, n_f = len(tails), len(records)
    n_vars = n_a * n_f
    constraints: list[tuple[list[int], list[float], float, float]] = []
    for fi in range(n_f):
        idx = [ai * n_f + fi for ai in range(n_a) if allowed[ai, fi]]
        if not idx:
            raise ValueError(
                f"No aircraft can serve flight {flight_ids[fi]!r} from its declared initial state/equipment"
            )
        constraints.append((idx, [1.0] * len(idx), 1.0, 1.0))

    # For each tail and incompatible flight pair, x[a,i] + x[a,j] <= 1.
    for ai in range(n_a):
        for i in range(n_f):
            if not allowed[ai, i]:
                continue
            for j in range(i + 1, n_f):
                if not allowed[ai, j]:
                    continue
                first, second = sorted(
                    (records[i], records[j]), key=lambda f: (f["STD"], str(f["flight_id"]))
                )
                if not _can_follow(first, second):
                    constraints.append((
                        [ai * n_f + i, ai * n_f + j], [1.0, 1.0], 0.0, 1.0
                    ))

    matrix = lil_matrix((len(constraints), n_vars), dtype=float)
    lower = np.empty(len(constraints), dtype=float)
    upper = np.empty(len(constraints), dtype=float)
    for row, (indices, values, lo, hi) in enumerate(constraints):
        for idx, value in zip(indices, values):
            matrix[row, idx] = value
        lower[row], upper[row] = lo, hi

    costs = np.zeros(n_vars, dtype=float)
    upper_bounds = np.zeros(n_vars, dtype=float)
    for ai, tail in enumerate(tails):
        for fi in range(n_f):
            idx = ai * n_f + fi
            costs[idx] = float(tail.start_cost)
            upper_bounds[idx] = 1.0 if allowed[ai, fi] else 0.0
    options = {"disp": False}
    if time_limit_seconds is not None:
        if not isfinite(float(time_limit_seconds)) or time_limit_seconds <= 0:
            raise ValueError("time_limit_seconds must be finite and positive")
        options["time_limit"] = float(time_limit_seconds)
    result = milp(
        c=costs,
        integrality=np.ones(n_vars, dtype=np.int32),
        bounds=Bounds(np.zeros(n_vars), upper_bounds),
        constraints=LinearConstraint(matrix.tocsr(), lower, upper),
        options=options,
    )
    if result.x is None or result.status != 0:
        raise RuntimeError(
            f"Aircraft rotation assignment did not prove an optimal feasible assignment: "
            f"status={result.status}; {result.message}"
        )

    assignments: dict[str, str] = {}
    rotations: dict[str, list[str]] = {tail.tail_id: [] for tail in tails}
    flight_by_id = {str(f["flight_id"]): f for f in records}
    for ai, tail in enumerate(tails):
        for fi, flight in enumerate(records):
            if result.x[ai * n_f + fi] > 0.5:
                fid = flight_ids[fi]
                assignments[fid] = tail.tail_id
                rotations[tail.tail_id].append(fid)
    for tail_id, rotation in rotations.items():
        rotation.sort(key=lambda fid: flight_by_id[fid]["STD"])
    if set(assignments) != set(flight_ids):
        raise RuntimeError("Aircraft assignment omitted one or more operated flights")
    return AircraftRotationResult(
        assignments=assignments,
        rotations={tail: tuple(ids) for tail, ids in rotations.items()},
        objective=float(result.fun),
        solver_message=str(result.message),
    )
