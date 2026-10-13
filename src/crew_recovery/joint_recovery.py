"""Joint crew-pairing and aircraft-route MILP for operate/cancel recovery.

Aircraft route flow is aggregated by equipment class rather than duplicated for
every tail. Selected source arcs identify each provisional resource; the unique
flight-to-flight arcs then decompose into tail routes. This reduces the binary
network while preserving resource-specific start station/time constraints.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import timedelta
from math import isfinite
from typing import Mapping, Sequence

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import lil_matrix

from column_generation import PairingColumn
from crew_recovery.aircraft_rotation import Aircraft
from crew_recovery.connection_scoring import connection_priority_score


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
    cancellation_reasons: Mapping[str, str] = field(default_factory=dict)
    heuristic_diagnostics: Mapping[str, object] = field(default_factory=dict)


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
    arc_candidates: list[tuple[float, int, int, int]] = []
    incoming: dict[tuple[int, int], list[int]] = {(c, i): [] for c in range(n_c) for i in range(n_f)}
    outgoing: dict[tuple[int, int], list[int]] = {(c, i): [] for c in range(n_c) for i in range(n_f)}
    # Index potential successors by equipment class and departure station.
    # The previous all-pairs scan was O(equipment_classes * flights^2), making
    # the 154-day horizon impractical. Binary-search the earliest departure
    # that can satisfy arrival + MGT, then inspect only same-station candidates.
    successors_by_class_origin: dict[int, dict[str, tuple[list[object], list[int]]]] = {}
    for c, _eqp in enumerate(classes):
        by_origin: dict[str, list[tuple[object, int]]] = {}
        for j, flight in enumerate(records):
            if eligible[c, j]:
                station = str(flight["From"]).strip().upper()
                by_origin.setdefault(station, []).append((flight["STD"], j))
        successors_by_class_origin[c] = {
            station: (
                [entry[0] for entry in sorted(entries)],
                [entry[1] for entry in sorted(entries)],
            )
            for station, entries in by_origin.items()
        }

    for c, eqp in enumerate(classes):
        by_origin = successors_by_class_origin[c]
        for i, first in enumerate(records):
            if not eligible[c, i]:
                continue
            station = str(first["To"]).strip().upper()
            indexed = by_origin.get(station)
            if indexed is None:
                continue
            departure_times, candidate_indices = indexed
            earliest = first["STA"] + timedelta(
                minutes=_mgt_minutes(first.get("mgt_min"), str(first["flight_id"]))
            )
            start_at = bisect_left(departure_times, earliest)
            for pos in range(start_at, len(candidate_indices)):
                j = candidate_indices[pos]
                if j <= i:
                    continue
                second = records[j]
                if _can_follow(first, second):
                    score = connection_priority_score(first, second)
                    arc_candidates.append((score, c, i, j))
    # Rank promising direct returns and short feasible ground gaps first, but
    # retain every legal arc. Scoring is not a hard pruning rule.
    arc_candidates.sort()
    arcs: list[tuple[int, int, int]] = []
    for _score, c, i, j in arc_candidates:
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
    cancelled = tuple(ids[i] for i in range(n_f) if solution[cancel0 + i] > 0.5)
    selected_source = {
        key: var for key, var in source_index.items() if solution[var] > 0.5
    }
    selected_next = {}
    for k, (c, i, j) in enumerate(arcs):
        if solution[arc_var[k]] > 0.5:
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
    pairing_cover = {fid: 0 for fid in ids}
    for p in columns:
        if p.column_id in selected:
            for fid in p.flight_ids:
                pairing_cover[fid] += 1
    bad_cover = [fid for fid in ids if pairing_cover[fid] != (0 if fid in cancelled else 1)]
    if bad_cover:
        raise RuntimeError(f"Internal solution audit: selected pairing cover is invalid for {bad_cover[:10]}")
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


def solve_joint_operate_cancel_greedy(
    flights: Sequence[Mapping[str, object]],
    pairings: Sequence[PairingColumn],
    aircraft: Sequence[Aircraft],
    *,
    preferred_aircraft_by_flight: Mapping[str, str] | None = None,
    max_retry_passes: int = 4,
) -> JointRecoveryResult:
    """Construct and compare bounded feasible recovery heuristics.

    The supplied AIRFLITE line assignment may be passed as a preference map.
    The function compares the previous single-pass baseline against retry-based
    selection that either prefers those line IDs or locks flights to those lines.
    Pairings rejected only because a prerequisite aircraft leg has not yet been
    inserted can be retried after other pairings have been accepted.

    All crew/MGT, equipment, starting-state, station-continuity, and route rules
    remain hard. These deterministic plans are heuristics, not global optima.
    """
    flight_rows = [dict(f) for f in flights]
    if not flight_rows:
        raise ValueError("flights must not be empty")
    flight_ids = [str(f.get("flight_id", "")).strip() for f in flight_rows]
    if any(not fid for fid in flight_ids) or len(flight_ids) != len(set(flight_ids)):
        raise ValueError("flight_id values must be unique and non-empty")
    records = {str(f["flight_id"]).strip(): f for f in flight_rows}

    tails = list(aircraft)
    if not tails:
        raise ValueError("aircraft inventory must not be empty")
    tail_by_id = {a.tail_id: a for a in tails}
    if len(tail_by_id) != len(tails):
        raise ValueError("aircraft tail_id values must be unique")
    if not isinstance(max_retry_passes, int) or max_retry_passes < 0:
        raise ValueError("max_retry_passes must be a nonnegative integer")

    preferred_map = {
        str(fid).strip(): str(tail_id).strip()
        for fid, tail_id in (preferred_aircraft_by_flight or {}).items()
        if str(fid).strip() in records
    }
    has_complete_line_map = (
        len(preferred_map) == len(records)
        and all(tail_id in tail_by_id for tail_id in preferred_map.values())
    )

    def flight_ids_for(pairing: PairingColumn) -> tuple[str, ...]:
        return tuple(str(fid) for fid in pairing.flight_ids)

    column_ids = [p.column_id for p in pairings]
    if len(column_ids) != len(set(column_ids)):
        raise ValueError("pairing column IDs must be unique")

    usable_pairings = [
        p for p in pairings
        if p.flight_ids
        and len(set(flight_ids_for(p))) == len(flight_ids_for(p))
        and all(fid in records for fid in flight_ids_for(p))
    ]
    pairing_fids = {p.column_id: flight_ids_for(p) for p in usable_pairings}
    candidate_degree = {fid: 0 for fid in records}
    pairings_by_flight: dict[str, list[PairingColumn]] = {fid: [] for fid in records}
    for pairing in usable_pairings:
        for fid in pairing_fids[pairing.column_id]:
            candidate_degree[fid] += 1
            pairings_by_flight[fid].append(pairing)

    def can_follow(first: Mapping[str, object], second: Mapping[str, object]) -> bool:
        if str(first["To"]).strip().upper() != str(second["From"]).strip().upper():
            return False
        required = _mgt_minutes(first.get("mgt_min"), str(first["flight_id"]))
        return second["STD"] >= first["STA"] + timedelta(minutes=required)

    def insertion_position(
        route: list[dict[str, object]], flight: dict[str, object], tail: Aircraft
    ) -> tuple[int, float] | None:
        if str(flight["Eqp"]).strip() != tail.equipment.strip():
            return None
        key = (flight["STD"], str(flight["flight_id"]))
        pos = bisect_left(route, key, key=lambda item: (item["STD"], str(item["flight_id"])))
        if pos == 0:
            if str(flight["From"]).strip().upper() != tail.start_station.strip().upper():
                return None
            if flight["STD"] < tail.available_at:
                return None
        elif not can_follow(route[pos - 1], flight):
            return None
        if pos < len(route) and not can_follow(flight, route[pos]):
            return None
        added_gap = 0.0
        if pos > 0:
            added_gap += (flight["STD"] - route[pos - 1]["STA"]).total_seconds()
        if pos < len(route):
            added_gap += (route[pos]["STD"] - flight["STA"]).total_seconds()
        return pos, added_gap

    def earliest_start(pairing: PairingColumn):
        return min(records[fid]["STD"] for fid in pairing_fids[pairing.column_id])

    def salary_per_flight(pairing: PairingColumn) -> float:
        return float(pairing.cost) / max(1, len(pairing_fids[pairing.column_id]))

    orderings = {
        "chronological_first": sorted(
            usable_pairings,
            key=lambda p: (
                earliest_start(p), -len(pairing_fids[p.column_id]),
                salary_per_flight(p), p.column_id,
            ),
        ),
        "long_pairings_first": sorted(
            usable_pairings,
            key=lambda p: (
                -len(pairing_fids[p.column_id]), salary_per_flight(p),
                earliest_start(p), p.column_id,
            ),
        ),
        "rare_flights_first": sorted(
            usable_pairings,
            key=lambda p: (
                -sum(1.0 / max(1, candidate_degree[fid]) for fid in pairing_fids[p.column_id]),
                -len(pairing_fids[p.column_id]), salary_per_flight(p),
                earliest_start(p), p.column_id,
            ),
        ),
        "cost_per_flight_first": sorted(
            usable_pairings,
            key=lambda p: (
                salary_per_flight(p), -len(pairing_fids[p.column_id]),
                earliest_start(p), p.column_id,
            ),
        ),
    }

    def construct_plan(
        ordering_name: str,
        ordered: Sequence[PairingColumn],
        assignment_mode: str,
        retry_passes: int,
    ) -> dict[str, object]:
        routes: dict[str, list[dict[str, object]]] = {a.tail_id: [] for a in tails}
        covered: set[str] = set()
        assigned: dict[str, str] = {}
        selected: list[str] = []
        total_cost = 0.0
        pending = list(ordered)
        passes_used = 0

        for pass_number in range(retry_passes + 1):
            passes_used = pass_number + 1
            next_pending: list[PairingColumn] = []
            progress = False

            for pairing in pending:
                fids = pairing_fids[pairing.column_id]
                if not fids:
                    continue
                if any(fid in covered for fid in fids):
                    # This candidate overlaps an already selected pairing, so
                    # retrying it cannot help.
                    continue

                undo: list[tuple[str, int, str]] = []
                new_assignments: dict[str, str] = {}
                feasible = True
                sequence = sorted(
                    (records[fid] for fid in fids),
                    key=lambda f: (f["STD"], str(f["flight_id"])),
                )

                for flight in sequence:
                    fid = str(flight["flight_id"])
                    preferred_tail_id = preferred_map.get(fid)
                    options = []
                    if assignment_mode == "line_locked":
                        candidate_tails = (
                            [tail_by_id[preferred_tail_id]]
                            if preferred_tail_id in tail_by_id else []
                        )
                    elif assignment_mode == "line_preferred" and preferred_tail_id in tail_by_id:
                        # Fast path: if the original line can take the flight,
                        # preserve it without scoring all other aircraft. Only
                        # search alternative tails when the preferred line is blocked.
                        preferred_found = insertion_position(
                            routes[preferred_tail_id], flight, tail_by_id[preferred_tail_id]
                        )
                        if preferred_found is not None:
                            preferred_pos, preferred_gap = preferred_found
                            options = [(0, preferred_gap, len(routes[preferred_tail_id]), preferred_tail_id, preferred_pos)]
                            candidate_tails = []
                        else:
                            candidate_tails = [tail for tail in tails if tail.tail_id != preferred_tail_id]
                            options = []
                    else:
                        candidate_tails = tails
                        options = []

                    for tail in candidate_tails:
                        found = insertion_position(routes[tail.tail_id], flight, tail)
                        if found is None:
                            continue
                        pos, added_gap = found
                        preference_penalty = (
                            0 if tail.tail_id == preferred_tail_id else 1
                        ) if assignment_mode == "line_preferred" else 0
                        options.append((
                            preference_penalty, added_gap,
                            len(routes[tail.tail_id]), tail.tail_id, pos,
                        ))

                    if not options:
                        feasible = False
                        break

                    _pref, _gap, _size, tail_id, pos = min(
                        options, key=lambda x: (x[0], x[1], x[2], x[3])
                    )
                    routes[tail_id].insert(pos, flight)
                    undo.append((tail_id, pos, fid))
                    new_assignments[fid] = tail_id

                if not feasible:
                    for tail_id, pos, fid in reversed(undo):
                        if pos < len(routes[tail_id]) and str(routes[tail_id][pos]["flight_id"]) == fid:
                            routes[tail_id].pop(pos)
                        else:
                            raise RuntimeError("Greedy pairing rollback invariant failed")
                    next_pending.append(pairing)
                    continue

                assigned.update(new_assignments)
                covered.update(fids)
                selected.append(pairing.column_id)
                total_cost += float(pairing.cost)
                progress = True

            pending = next_pending
            if not pending or not progress:
                break

        used_tails = {assigned[fid] for fid in assigned}
        total_cost += sum(float(tail_by_id[tid].start_cost) for tid in used_tails)
        same_preferred_line = sum(
            1 for fid, tail_id in assigned.items()
            if fid in preferred_map and preferred_map[fid] == tail_id
        )
        return {
            "name": f"{assignment_mode}_{'single_pass' if retry_passes == 0 else 'retry'}__{ordering_name}",
            "ordering": ordering_name,
            "assignment_mode": assignment_mode,
            "covered": covered,
            "assigned": assigned,
            "selected": selected,
            "total_cost": total_cost,
            "passes_used": passes_used,
            "same_preferred_line_operated": same_preferred_line,
            "moved_from_preferred_line_operated": len(assigned) - same_preferred_line,
            "unresolved_pairings_after_retries": len(pending),
        }

    plans: list[dict[str, object]] = []
    # Keep the previous three one-pass strategies as exact comparators.
    historical_ordering_names = (
        "long_pairings_first", "rare_flights_first", "cost_per_flight_first"
    )
    for ordering_name in historical_ordering_names:
        plans.append(construct_plan(
            ordering_name, orderings[ordering_name], "free", retry_passes=0
        ))

    # Retry under the two orders most useful for repair: chronological order
    # builds predecessor routes first; cost-per-flight preserves the prior best
    # coverage result as an additional reference.
    retry_ordering_names = ("chronological_first", "cost_per_flight_first")
    for ordering_name in retry_ordering_names:
        ordering = orderings[ordering_name]
        plans.append(construct_plan(
            ordering_name, ordering, "free", retry_passes=max_retry_passes
        ))
        if preferred_map:
            plans.append(construct_plan(
                ordering_name, ordering, "line_preferred", retry_passes=max_retry_passes
            ))

    # A strict same-line plan is the cleanest diagnostic for whether the existing
    # supplied line baseline can carry the candidate pool without reconstruction.
    if has_complete_line_map:
        plans.append(construct_plan(
            "chronological_first", orderings["chronological_first"],
            "line_locked", retry_passes=max_retry_passes
        ))

    best = min(
        plans,
        key=lambda plan: (
            len(records) - len(plan["covered"]),
            float(plan["total_cost"]),
            tuple(sorted(plan["selected"])),
        ),
    )
    covered = best["covered"]
    assigned = best["assigned"]
    selected = best["selected"]
    cancelled = tuple(sorted(set(records) - covered))

    old_baseline = next(
        plan for plan in plans
        if plan["assignment_mode"] == "free"
        and plan["ordering"] == "long_pairings_first"
        and plan["passes_used"] == 1
    )
    plan_comparison = {
        str(plan["name"]): {
            "operated_flights": len(plan["covered"]),
            "cancelled_flights": len(records) - len(plan["covered"]),
            "secondary_objective": float(plan["total_cost"]),
            "assignment_mode": str(plan["assignment_mode"]),
            "pairing_order": str(plan["ordering"]),
            "passes_used": int(plan["passes_used"]),
            "same_preferred_line_operated": int(plan["same_preferred_line_operated"]),
            "moved_from_preferred_line_operated": int(plan["moved_from_preferred_line_operated"]),
            "unresolved_pairings_after_retries": int(plan["unresolved_pairings_after_retries"]),
        }
        for plan in plans
    }
    coverage_gain = len(covered) - len(old_baseline["covered"])
    heuristic_diagnostics = {
        "plans_evaluated": len(plans),
        "single_pass_baseline": plan_comparison[old_baseline["name"]],
        "best_plan": plan_comparison[str(best["name"])],
        "operated_flights_gain_vs_single_pass_baseline": coverage_gain,
        "coverage_percentage_point_gain_vs_single_pass_baseline": round(
            100.0 * coverage_gain / max(1, len(records)), 2
        ),
        "preferred_aircraft_line_map_complete": bool(has_complete_line_map),
        "preferred_aircraft_line_map_flights": int(len(preferred_map)),
        "evaluated_plans": plan_comparison,
        "comparison_note": (
            "The single-pass baseline and new retry/line-preservation strategies use the same "
            "flight set, pairing candidate pool and aircraft inventory. All plans are independently "
            "checked by the runner's post-solve aircraft and crew coverage validators. The line-locked "
            "variant is only a diagnostic when provisional AIRFLITE line IDs are used; those IDs are "
            "not verified physical tail registrations."
        ),
    }

    reasons: dict[str, str] = {}
    for fid in cancelled:
        candidates = pairings_by_flight[fid]
        if not candidates:
            reasons[fid] = "NO_VALID_PAIRING_CANDIDATE"
            continue
        all_blocked_by_overlap = all(
            any(other != fid and other in covered for other in pairing_fids[p.column_id])
            for p in candidates
        )
        reasons[fid] = (
            "PAIRING_OVERLAP_WITH_SELECTED_PAIRING"
            if all_blocked_by_overlap
            else "NO_FEASIBLE_AIRCRAFT_ROUTE_FOR_AVAILABLE_PAIRING"
        )

    return JointRecoveryResult(
        cancelled_flights=cancelled,
        selected_pairing_ids=tuple(sorted(selected)),
        aircraft_by_flight=dict(assigned),
        cancellation_count=len(cancelled),
        secondary_objective=float(best["total_cost"]),
        solver_message=(
            f"Best of {len(plans)} deterministic feasible heuristic plans: "
            f"{best['name']}; operates {len(covered)}/{len(records)} flights. "
            "Hard route feasibility constraints are unchanged; no global optimum is claimed."
        ),
        optimal_over_supplied_columns=False,
        minimum_cancellation_count_proven=False,
        retiming_supported=False,
        cancellation_reasons=reasons,
        heuristic_diagnostics=heuristic_diagnostics,
    )
