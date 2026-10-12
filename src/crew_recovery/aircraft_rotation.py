"""Aircraft rotation assignment using explicit aircraft-specific route flow."""
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
    """Assign each flight to one tail and enforce a connected chronological route.

    Tail equipment compatibility defaults to exact code equality. Each tail
    starts at its declared station/time; subsequent flights must connect from
    the preceding flight's destination with that flight's MGT. The model uses
    source, flight-to-flight, and sink arcs, so it does not require every flight
    on a tail to connect directly to every other flight.
    """
    records = [dict(f) for f in flights]
    tails = list(aircraft)
    if not records or not tails:
        raise ValueError("flights and aircraft inventory must both be non-empty")
    flight_ids = [str(f.get("flight_id", "")) for f in records]
    if any(not fid for fid in flight_ids) or len(set(flight_ids)) != len(flight_ids):
        raise ValueError("flight_id values must be unique and non-empty")
    tail_ids = [a.tail_id for a in tails]
    if len(set(tail_ids)) != len(tail_ids):
        raise ValueError("tail_id values must be unique")
    for f in records:
        for key in ("From", "To", "STD", "STA", "Eqp"):
            if f.get(key) is None:
                raise ValueError(f"flight {f.get('flight_id')!r} is missing {key}")
        _minutes(f.get("mgt_min", f.get("MGT_td")))

    records.sort(key=lambda f: (f["STD"], str(f["flight_id"])))
    flight_ids = [str(f["flight_id"]) for f in records]
    n_a, n_f = len(tails), len(records)
    compatibility = {
        str(k).strip(): {str(v).strip() for v in values}
        for k, values in (compatible_equipment or {}).items()
    }
    accepted_codes = [
        compatibility.get(tail.equipment, {tail.equipment})
        for tail in tails
    ]
    eligible = np.zeros((n_a, n_f), dtype=bool)
    source_ok = np.zeros((n_a, n_f), dtype=bool)
    for ai, tail in enumerate(tails):
        for fi, flight in enumerate(records):
            code_ok = str(flight["Eqp"]).strip() in accepted_codes[ai]
            eligible[ai, fi] = code_ok
            source_ok[ai, fi] = (
                code_ok
                and str(flight["From"]).strip().upper() == tail.start_station.strip().upper()
                and flight["STD"] >= tail.available_at
            )

    for fi, fid in enumerate(flight_ids):
        if not eligible[:, fi].any():
            raise ValueError(f"No aircraft has compatible equipment for flight {fid!r}")

    # Variable blocks: assign x[a,i], source s[a,i], sink t[a,i], arcs y[a,i,j].
    x0 = 0
    s0 = n_a * n_f
    t0 = 2 * n_a * n_f
    arc_list = [
        (ai, i, j)
        for ai, tail in enumerate(tails)
        for i in range(n_f)
        for j in range(n_f)
        if i != j and records[i]["STD"] < records[j]["STD"]
        and eligible[ai, i] and eligible[ai, j]
        and _can_follow(records[i], records[j])
    ]
    y0 = 3 * n_a * n_f
    n_vars = y0 + len(arc_list)
    arc_index = {arc: y0 + k for k, arc in enumerate(arc_list)}
    constraints: list[tuple[dict[int, float], float, float]] = []

    # Every flight is assigned to exactly one tail.
    for i in range(n_f):
        constraints.append(({ai * n_f + i: 1.0 for ai in range(n_a)}, 1.0, 1.0))

    for ai in range(n_a):
        # A tail can start at most one rotation and end at most one rotation.
        constraints.append(({s0 + ai * n_f + i: 1.0 for i in range(n_f)}, 0.0, 1.0))
        constraints.append(({t0 + ai * n_f + i: 1.0 for i in range(n_f)}, 0.0, 1.0))
        for i in range(n_f):
            x_idx = x0 + ai * n_f + i
            s_idx = s0 + ai * n_f + i
            t_idx = t0 + ai * n_f + i
            incoming = {s_idx: -1.0, x_idx: 1.0}
            outgoing = {t_idx: -1.0, x_idx: 1.0}
            for (a, j, k), idx in arc_index.items():
                if a == ai and k == i:
                    incoming[idx] = -1.0
                if a == ai and j == i:
                    outgoing[idx] = -1.0
            # x = source + incoming arcs; x = sink + outgoing arcs.
            constraints.append((incoming, 0.0, 0.0))
            constraints.append((outgoing, 0.0, 0.0))
            if not eligible[ai, i]:
                constraints.append(({x_idx: 1.0}, 0.0, 0.0))
            if not source_ok[ai, i]:
                constraints.append(({s_idx: 1.0}, 0.0, 0.0))

    matrix = lil_matrix((len(constraints), n_vars), dtype=float)
    lower = np.empty(len(constraints), dtype=float)
    upper = np.empty(len(constraints), dtype=float)
    for row, (terms, lo, hi) in enumerate(constraints):
        for idx, value in terms.items():
            matrix[row, idx] = value
        lower[row], upper[row] = lo, hi

    costs = np.zeros(n_vars, dtype=float)
    for ai, tail in enumerate(tails):
        for i in range(n_f):
            costs[s0 + ai * n_f + i] = float(tail.start_cost)
    upper_bounds = np.ones(n_vars, dtype=float)
    for ai in range(n_a):
        for i in range(n_f):
            if not eligible[ai, i]:
                upper_bounds[x0 + ai * n_f + i] = 0.0
            if not source_ok[ai, i]:
                upper_bounds[s0 + ai * n_f + i] = 0.0
                upper_bounds[t0 + ai * n_f + i] = 1.0

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
    for ai, tail in enumerate(tails):
        for i, fid in enumerate(flight_ids):
            if result.x[x0 + ai * n_f + i] > 0.5:
                assignments[fid] = tail.tail_id
                rotations[tail.tail_id].append(fid)
    if set(assignments) != set(flight_ids):
        raise RuntimeError("Aircraft assignment omitted one or more operated flights")
    return AircraftRotationResult(
        assignments=assignments,
        rotations={tail: tuple(ids) for tail, ids in rotations.items()},
        objective=float(result.fun),
        solver_message=str(result.message),
    )
