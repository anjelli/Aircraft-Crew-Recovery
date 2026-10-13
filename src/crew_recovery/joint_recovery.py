"""Joint crew-pairing and aircraft-route MILP for operate/cancel recovery.

Aircraft route flow is aggregated by equipment class rather than duplicated for
every tail. Selected source arcs identify each provisional resource; the unique
flight-to-flight arcs then decompose into tail routes. This reduces the binary
network while preserving resource-specific start station/time constraints.
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
    minimum_cancellation_count_proven: bool = True
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
    to exactly one aircraft resource, or cancelled. Aircraft paths enforce
    start station/time, equipment compatibility, station continuity and MGT.
    The first solve proves the minimum cancellation count; the second fixes that
    count and minimizes pairing costs plus resource start costs.

    This baseline supports operate/cancel only. Retiming must be represented by
    explicit flight copies and is not silently approximated. Optimality is only
    over the supplied pairing columns and generated route arcs.
    """
    records = [dict(f) for f in flights]
    if not records:
        raise ValueError("flights must not be empty")
    ids = [str(f.get("flight_id", "")).strip() for f in records]
    if any(not fid for fid in ids) or len(ids) != len(set(ids)):
        raise ValueError("flight_id values must be unique and non-empty")
    for f in records:
        for key in ("From", "To", "STD", "STA", "Eqp"):
            if f.get(key) is None:
                raise ValueError(f"Flight {f['flight_id']!r} is missing {key}")
        _mgt_minutes(f.get("mgt_min"), str(f["flight_id"]))
    records.sort(key=lambda f: (f["STD"], str(f["flight_id"])))
    ids = [str(f["flight_id"]) for f in records]
    index = {fid: i for i, fid in enumerate(ids)}
    n_f = len(records)

    tails = list(aircraft)
    if not tails:
        raise ValueError("aircraft inventory must not be empty")
    tail_ids = [a.tail_id for a in tails]
    if len(tail_ids) != len(set(tail_ids)):
        raise ValueError("aircraft tail_id values must be unique")
    classes = sorted({a.equipment for a in tails})
    class_index = {eqp: i for i, eqp in enumerate(classes)}
    tail_class = [class_index[a.equipment] for a in tails]
    n_c = len(classes)

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
    accepted = {
        eqp: compatibility.get(eqp, {eqp})
        for eqp in classes
    }
    eligible = {
        (c, i): str(records[i]["Eqp"]).strip() in accepted[eqp]
        for c, eqp in enumerate(classes)
        for i in range(n_f)
    }
    start_ok: dict[tuple[int, int], bool] = {}
    for a, tail in enumerate(tails):
        for i, f in enumerate(records):
            start_ok[a, i] = (
                eligible[tail_class[a], i]
                and str(f["From"]).strip().upper() == tail.start_station.strip().upper()
                and f["STD"] >= tail.available_at
            )

    # A single route network per equipment class; each selected source arc
    # starts one path from a specific resource, so tail identities are recovered
    # by tracing selected arcs from those source flights.
    arcs: list[tuple[int, int, int]] = []
    incoming: dict[tuple[int, int], list[int]] = {(c, i): [] for c in range(n_c) for i in range(n_f)}
    outgoing: dict[tuple[int, int], list[int]] = {(c, i): [] for c in range(n_c) for i in range(n_f)}
    for c, eqp in enumerate(classes):
        for i, first in enumerate(records):
            if not eligible[c, i]:
                continue
            for j in range(i + 1, n_f):
                if not eligible[c, j]:
                    continue
                if records[j]["STD"] < first["STA"]:
                    continue
                if _can_follow(first, records[j]):
                    k = len(arcs)
                    arcs.append((c, i, j))
                    outgoing[c, i].append(k)
                    incoming[c, j].append(k)

    # Variables: pairing selection x, cancellation c, class-flight y,
    # resource-specific source arcs s, and equipment-class route arcs z.
    n_p = len(columns)
    x0 = 0
    cancel0 = x0 + n_p
    y0 = cancel0 + n_f
    source_keys = [(a, i) for a in range(len(tails)) for i in range(n_f) if start_ok[a, i]]
    source0 = y0 + n_c * n_f
    source_index = {key: source0 + k for k, key in enumerate(source_keys)}
    arc0 = source0 + len(source_keys)
    arc_var = {k: arc0 + k for k in range(len(arcs))}
    n_var = arc0 + len(arcs)

    def yvar(c: int, i: int) -> int:
        return y0 + c * n_f + i

    eq_rows = n_f + n_f + n_c * n_f
    ub_rows = n_c * n_f + len(tails)
    a_eq = lil_matrix((eq_rows, n_var), dtype=float)
    b_eq = np.ones(eq_rows, dtype=float)
    b_eq[2 * n_f:] = 0.0
    a_ub = lil_matrix((ub_rows, n_var), dtype=float)
    b_ub = np.zeros(ub_rows, dtype=float)

    # Crew cover: selected pairing coverage + cancellation = 1.
    for p_idx, p in enumerate(columns):
        for fid in p.flight_ids:
            a_eq[index[fid], x0 + p_idx] = 1.0
    for i in range(n_f):
        a_eq[i, cancel0 + i] = 1.0

    # Aircraft cover: exactly one equipment-class assignment or cancellation.
    aircraft_cover_row = n_f
    for i in range(n_f):
        for c in range(n_c):
            a_eq[aircraft_cover_row + i, yvar(c, i)] = 1.0
        a_eq[aircraft_cover_row + i, cancel0 + i] = 1.0

    # Each class-flight assignment has one source or predecessor.
    path_row0 = 2 * n_f
    for c in range(n_c):
        class_tails = [a for a in range(len(tails)) if tail_class[a] == c]
        for i in range(n_f):
            row = path_row0 + c * n_f + i
            a_eq[row, yvar(c, i)] = 1.0
            for a in class_tails:
                if (a, i) in source_index:
                    a_eq[row, source_index[a, i]] = -1.0
            for k in incoming[c, i]:
                a_eq[row, arc_var[k]] = -1.0

    # Each assigned flight has at most one successor. Each resource starts at
    # most one path.
    for c in range(n_c):
        for i in range(n_f):
            row = c * n_f + i
            a_ub[row, yvar(c, i)] = -1.0
            for k in outgoing[c, i]:
                a_ub[row, arc_var[k]] = 1.0
    for a in range(len(tails)):
        row = n_c * n_f + a
        for key, var in source_index.items():
            if key[0] == a:
                a_ub[row, var] = 1.0
        b_ub[row] = 1.0

    integrality = np.ones(n_var, dtype=int)
    bounds = Bounds(np.zeros(n_var), np.ones(n_var))
    constraints = [
        LinearConstraint(a_eq.tocsr(), b_eq, b_eq),
        LinearConstraint(a_ub.tocsr(), -np.inf, b_ub),
    ]
    options = {"mip_rel_gap": 0.0}
    if time_limit_seconds is not None:
        if not isfinite(float(time_limit_seconds)) or time_limit_seconds <= 0:
            raise ValueError("time_limit_seconds must be finite and positive")
        options["time_limit"] = float(time_limit_seconds)

    secondary = np.zeros(n_var, dtype=float)
    for p_idx, p in enumerate(columns):
        secondary[x0 + p_idx] = float(p.cost)
    for (a, _i), var in source_index.items():
        secondary[var] = float(tails[a].start_cost)

    # Pass 1: prove the minimum cancellation count. If the time limit returns a
    # feasible integer incumbent, preserve it as a clearly labelled feasible
    # plan rather than discarding it or pretending it is optimal.
    cancellation_objective = np.zeros(n_var, dtype=float)
    cancellation_objective[cancel0:cancel0 + n_f] = 1.0
    first = milp(c=cancellation_objective, integrality=integrality, bounds=bounds, constraints=constraints, options=options)
    if first.x is None:
        raise RuntimeError(f"Minimum-cancellation pass returned no feasible incumbent: {first.message}")
    cancellation_optimal = first.status == 0
    min_cancellations = int(round(float(np.sum(first.x[cancel0:cancel0 + n_f]))))
    solution = first.x
    secondary_optimal = False
    solver_message = str(first.message)

    if cancellation_optimal:
        cancel_row = lil_matrix((1, n_var), dtype=float)
        for i in range(n_f):
            cancel_row[0, cancel0 + i] = 1.0
        cancel_constraint = LinearConstraint(cancel_row.tocsr(), [min_cancellations], [min_cancellations])
        second = milp(
            c=secondary, integrality=integrality, bounds=bounds,
            constraints=constraints + [cancel_constraint], options=options,
        )
        if second.x is not None:
            solution = second.x
            secondary_optimal = second.status == 0
            solver_message = str(second.message) if secondary_optimal else (
                "Minimum cancellation count proven optimal; secondary objective not proven optimal: "
                + str(second.message)
            )
        else:
            solver_message = (
                "Minimum cancellation count proven optimal; secondary pass returned no incumbent: "
                + str(second.message)
            )
    else:
        solver_message = (
            "Feasible incumbent returned, but minimum cancellation count is not proven optimal: "
            + str(first.message)
        )

    selected = tuple(columns[p].column_id for p in range(n_p) if solution[x0 + p] > 0.5)
    cancelled = tuple(ids[i] for i in range(n_f) if second.x[cancel0 + i] > 0.5)
    selected_source = {
        key: var for key, var in source_index.items() if second.x[var] > 0.5
    }
    selected_next = {}
    for k, (c, i, j) in enumerate(arcs):
        if second.x[arc_var[k]] > 0.5:
            if i in selected_next:
                raise RuntimeError(f"Internal solution audit: multiple successors for flight {ids[i]!r}")
            selected_next[i] = j

    aircraft_by_flight: dict[str, str] = {}
    for (a, first_i) in selected_source:
        current = first_i
        while True:
            fid = ids[current]
            if fid in aircraft_by_flight:
                raise RuntimeError(f"Internal solution audit: flight {fid!r} occurs on multiple aircraft paths")
            aircraft_by_flight[fid] = tails[a].tail_id
            if current not in selected_next:
                break
            next_i = selected_next[current]
            if not eligible[tail_class[a], next_i]:
                raise RuntimeError(f"Internal solution audit: incompatible route for {tails[a].tail_id!r}")
            current = next_i

    operated = set(ids) - set(cancelled)
    if set(aircraft_by_flight) != operated:
        raise RuntimeError(
            "Internal solution audit: aircraft path decomposition differs from operated flights "
            f"(missing={sorted(operated-set(aircraft_by_flight))[:10]}, "
            f"extra={sorted(set(aircraft_by_flight)-operated)[:10]})"
        )

    return JointRecoveryResult(
        cancelled_flights=cancelled,
        selected_pairing_ids=selected,
        aircraft_by_flight=aircraft_by_flight,
        cancellation_count=len(cancelled),
        secondary_objective=float(np.dot(secondary, solution)),
        solver_message=solver_message,
        optimal_over_supplied_columns=bool(cancellation_optimal and secondary_optimal),
        minimum_cancellation_count_proven=bool(cancellation_optimal),
    )
