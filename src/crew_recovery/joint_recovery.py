"""Joint crew-pairing and aircraft-route MILP for operate/cancel recovery.

This is the first coupled baseline: it jointly chooses validated pairing columns,
flight cancellations, and aircraft paths. It intentionally does not yet create
retimed flight copies. Optimality is only over the supplied pairing columns and
the route arcs generated from the supplied flight/resource data.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from math import isfinite
from typing import Mapping, Sequence

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import lil_matrix

from column_generation import PairingColumn
from crew_recovery.aircraft_rotation import Aircraft


@dataclass(frozen=True)
class JointRecoveryResult:
    cancelled_flights: tuple[str, ...]
    selected_pairing_ids: tuple[str, ...]
    aircraft_by_flight: Mapping[str, str]
    cancellation_count: int
    secondary_objective: float
    solver_message: str
    optimal_over_supplied_columns: bool = True
    retiming_supported: bool = False


def _mgt_minutes(value: object, flight_id: str) -> float:
    if value is None:
        raise ValueError(f"Flight {flight_id!r} is missing applicable mgt_min")
    if hasattr(value, "total_seconds"):
        minutes = float(value.total_seconds()) / 60.0
    else:
        minutes = float(value)
    if not isfinite(minutes) or minutes < 0:
        raise ValueError(f"Flight {flight_id!r} has invalid mgt_min={value!r}")
    return minutes


def _can_follow(first: Mapping[str, object], second: Mapping[str, object]) -> bool:
    if str(first["To"]).strip().upper() != str(second["From"]).strip().upper():
        return False
    gap = (second["STD"] - first["STA"]).total_seconds() / 60.0
    return gap + 1e-9 >= _mgt_minutes(first.get("mgt_min"), str(first["flight_id"]))


def solve_joint_operate_cancel(
    flights: Sequence[Mapping[str, object]],
    pairings: Sequence[PairingColumn],
    aircraft: Sequence[Aircraft],
    *,
    compatible_equipment: Mapping[str, Sequence[str]] | None = None,
    time_limit_seconds: float | None = None,
) -> JointRecoveryResult:
    """Jointly select crew pairings, cancellations and aircraft paths.

    Every flight is either covered by exactly one selected pairing and assigned
    to exactly one aircraft, or cancelled. Aircraft start arcs must respect
    declared start station/time; route arcs enforce station continuity, MGT and
    equipment compatibility. Missing MGT or an unproven optimum fails closed.

    This baseline supports operate/cancel decisions only. Retiming must be
    represented by explicit flight copies and is not silently approximated.
    """
    records = [dict(f) for f in flights]
    if not records:
        raise ValueError("flights must not be empty")
    ids = [str(f.get("flight_id", "")).strip() for f in records]
    if any(not fid for fid in ids) or len(set(ids)) != len(ids):
        raise ValueError("flight_id values must be unique and non-empty")
    by_id = {str(f["flight_id"]): f for f in records}
    for f in records:
        for key in ("From", "To", "STD", "STA", "Eqp"):
            if f.get(key) is None:
                raise ValueError(f"Flight {f['flight_id']!r} is missing {key}")
        _mgt_minutes(f.get("mgt_min"), str(f["flight_id"]))
    records.sort(key=lambda f: (f["STD"], str(f["flight_id"])))
    ids = [str(f["flight_id"]) for f in records]
    index = {fid: i for i, fid in enumerate(ids)}
    tails = list(aircraft)
    if not tails:
        raise ValueError("aircraft inventory must not be empty")
    tail_ids = [a.tail_id for a in tails]
    if len(tail_ids) != len(set(tail_ids)):
        raise ValueError("aircraft tail_id values must be unique")

    columns = list(pairings)
    column_ids = [p.column_id for p in columns]
    if len(column_ids) != len(set(column_ids)):
        raise ValueError("pairing column IDs must be unique")
    for p in columns:
        unknown = set(p.flight_ids) - set(ids)
        if unknown:
            raise ValueError(f"Pairing {p.column_id!r} references unknown flights: {sorted(unknown)}")

    compatibility = {
        str(k).strip(): {str(v).strip() for v in values}
        for k, values in (compatible_equipment or {}).items()
    }
    eligible = {
        (a, i): str(records[i]["Eqp"]).strip()
        in compatibility.get(tail.equipment, {tail.equipment})
        for a, tail in enumerate(tails)
        for i in range(len(records))
    }
    start_ok: dict[tuple[int, int], bool] = {}
    for a, tail in enumerate(tails):
        available = tail.available_at
        for i, f in enumerate(records):
            start_ok[a, i] = (
                eligible[a, i]
                and str(f["From"]).strip().upper() == tail.start_station.strip().upper()
                and f["STD"] >= available
            )

    # Only chronologically feasible route arcs are materialized.
    arcs: list[tuple[int, int, int]] = []
    incoming: dict[tuple[int, int], list[int]] = {(a, i): [] for a in range(len(tails)) for i in range(len(records))}
    outgoing: dict[tuple[int, int], list[int]] = {(a, i): [] for a in range(len(tails)) for i in range(len(records))}
    for a, tail in enumerate(tails):
        for i, first in enumerate(records):
            if not eligible[a, i]:
                continue
            for j in range(i + 1, len(records)):
                second = records[j]
                if not eligible[a, j]:
                    continue
                if second["STD"] < first["STA"]:
                    continue
                if _can_follow(first, second):
                    arc_index = len(arcs)
                    arcs.append((a, i, j))
                    outgoing[a, i].append(arc_index)
                    incoming[a, j].append(arc_index)

    # Variables: pairing selection x, cancellation c, aircraft assignment y,
    # valid source arcs s, and aircraft route arcs z.
    n_p, n_f, n_a = len(columns), len(records), len(tails)
    x0 = 0
    c0 = x0 + n_p
    y0 = c0 + n_f
    s0 = y0 + n_a * n_f
    source_keys = [(a, i) for a in range(n_a) for i in range(n_f) if start_ok[a, i]]
    source_index = {key: s0 + k for k, key in enumerate(source_keys)}
    z0 = s0 + len(source_keys)
    arc_var = {k: z0 + k for k in range(len(arcs))}
    n_var = z0 + len(arcs)

    def yvar(a: int, i: int) -> int:
        return y0 + a * n_f + i

    eq_rows = n_f + n_f + n_a * n_f
    ub_rows = n_a * n_f
    a_eq = lil_matrix((eq_rows, n_var), dtype=float)
    b_eq = np.ones(eq_rows, dtype=float)
    a_ub = lil_matrix((ub_rows, n_var), dtype=float)
    b_ub = np.zeros(ub_rows, dtype=float)

    # Crew cover: selected pairing coverage + cancellation = 1.
    for p_idx, p in enumerate(columns):
        for fid in p.flight_ids:
            a_eq[index[fid], x0 + p_idx] = 1.0
    for i in range(n_f):
        a_eq[i, c0 + i] = 1.0

    # Aircraft cover: aircraft assignment + the same cancellation variable = 1.
    aircraft_cover_row = n_f
    for i in range(n_f):
        for a in range(n_a):
            a_eq[aircraft_cover_row + i, yvar(a, i)] = 1.0
        a_eq[aircraft_cover_row + i, c0 + i] = 1.0

    # Every assigned flight has one source or predecessor on its aircraft path.
    path_row0 = 2 * n_f
    for a in range(n_a):
        for i in range(n_f):
            row = path_row0 + a * n_f + i
            a_eq[row, yvar(a, i)] = 1.0
            if (a, i) in source_index:
                a_eq[row, source_index[a, i]] = -1.0
            for k in incoming[a, i]:
                a_eq[row, arc_var[k]] = -1.0

    # At most one successor per assigned flight.
    for a in range(n_a):
        for i in range(n_f):
            row = a * n_f + i
            a_ub[row, yvar(a, i)] = -1.0
            for k in outgoing[a, i]:
                a_ub[row, arc_var[k]] = 1.0

    integrality = np.ones(n_var, dtype=int)
    bounds = Bounds(np.zeros(n_var), np.ones(n_var))
    eq_constraint = LinearConstraint(a_eq.tocsr(), b_eq, b_eq)
    ub_constraint = LinearConstraint(a_ub.tocsr(), -np.inf, b_ub)
    options = {"mip_rel_gap": 0.0}
    if time_limit_seconds is not None:
        if not isfinite(float(time_limit_seconds)) or time_limit_seconds <= 0:
            raise ValueError("time_limit_seconds must be finite and positive")
        options["time_limit"] = float(time_limit_seconds)

    # Pass 1: prove the minimum cancellation count.
    cancellation_objective = np.zeros(n_var, dtype=float)
    cancellation_objective[c0:c0 + n_f] = 1.0
    first = milp(
        c=cancellation_objective,
        integrality=integrality,
        bounds=bounds,
        constraints=[eq_constraint, ub_constraint],
        options=options,
    )
    if first.x is None or first.status != 0:
        raise RuntimeError(f"Minimum-cancellation pass not proven optimal: {first.message}")
    min_cancellations = int(round(float(np.sum(first.x[c0:c0 + n_f]))))

    # Pass 2: freeze the cancellation optimum, then minimize pairing costs and
    # aircraft start costs. This is a lexicographic second stage, not a big-M.
    cancel_row = lil_matrix((1, n_var), dtype=float)
    cancel_row[0, c0:c0 + n_f] = 1.0
    cancel_constraint = LinearConstraint(cancel_row.tocsr(), [min_cancellations], [min_cancellations])
    secondary = np.zeros(n_var, dtype=float)
    for p_idx, p in enumerate(columns):
        secondary[x0 + p_idx] = float(p.cost)
    for key, var in source_index.items():
        secondary[var] = float(tails[key[0]].start_cost)
    second = milp(
        c=secondary,
        integrality=integrality,
        bounds=bounds,
        constraints=[eq_constraint, ub_constraint, cancel_constraint],
        options=options,
    )
    if second.x is None or second.status != 0:
        raise RuntimeError(f"Secondary pass not proven optimal: {second.message}")

    selected = tuple(
        columns[p].column_id
        for p in range(n_p)
        if second.x[x0 + p] > 0.5
    )
    cancelled = tuple(
        ids[i] for i in range(n_f) if second.x[c0 + i] > 0.5
    )
    aircraft_by_flight = {}
    for i, fid in enumerate(ids):
        if fid in cancelled:
            continue
        assigned = [tails[a].tail_id for a in range(n_a) if second.x[yvar(a, i)] > 0.5]
        if len(assigned) != 1:
            raise RuntimeError(f"Internal solution audit: {fid!r} has {len(assigned)} aircraft assignments")
        aircraft_by_flight[fid] = assigned[0]

    return JointRecoveryResult(
        cancelled_flights=cancelled,
        selected_pairing_ids=selected,
        aircraft_by_flight=aircraft_by_flight,
        cancellation_count=len(cancelled),
        secondary_objective=float(second.fun),
        solver_message=str(second.message),
    )
