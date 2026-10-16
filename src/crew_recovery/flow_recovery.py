"""Exact aircraft-flow solver for the joint operate/cancel recovery problem.

Same hard rules as the joint MILP and the greedy heuristic (equipment match,
station continuity, MGT, tail start state, no repositioning, all-or-nothing
crew pairings) but modelled as a time-expanded network flow per equipment
family. It scales to the 154-day horizon (about a second to a few seconds with
HiGHS) where the route-variable MILP cannot find an incumbent and the greedy
heuristic leaves thousands of flights cancelled.

Stage 1 maximises operated flights. Stage 2 fixes that count and minimises
selected pairing cost. Aircraft start cost is added to the reported secondary
objective afterwards; it is not optimised here (it is 0 for provisional lines).
"""
from __future__ import annotations

import collections
import time
from datetime import timedelta
from typing import Mapping, Sequence

import numpy as np
import scipy.sparse as sp
from scipy.optimize import Bounds, LinearConstraint, milp

from column_generation import PairingColumn
from crew_recovery.aircraft_rotation import Aircraft
from crew_recovery.joint_recovery import JointRecoveryResult, _mgt_minutes


def _u(value: object) -> str:
    return str(value).strip().upper()


def solve_joint_operate_cancel_flow(
    flights: Sequence[Mapping[str, object]],
    pairings: Sequence[PairingColumn],
    aircraft: Sequence[Aircraft],
    *,
    time_limit_seconds: float = 300.0,
    secondary_time_limit_seconds: float = 120.0,
) -> JointRecoveryResult:
    """Return the maximum-coverage plan over the supplied pairing columns."""
    rows_in = [dict(f) for f in flights]
    if not rows_in:
        raise ValueError("flights must not be empty")
    ids = [str(f.get("flight_id", "")).strip() for f in rows_in]
    if any(not fid for fid in ids) or len(ids) != len(set(ids)):
        raise ValueError("flight_id values must be unique and non-empty")
    records = {fid: f for fid, f in zip(ids, rows_in)}
    mgt = {fid: timedelta(minutes=_mgt_minutes(f.get("mgt_min"), fid)) for fid, f in records.items()}
    columns = list(pairings)
    if len({p.column_id for p in columns}) != len(columns):
        raise ValueError("pairing column_id values must be unique")
    families = sorted({_u(a.equipment) for a in aircraft})

    n_p = len(columns)
    by_flight: dict[str, list[int]] = collections.defaultdict(list)
    for i, p in enumerate(columns):
        for fid in p.flight_ids:
            if fid not in records:
                raise ValueError(f"Pairing {p.column_id!r} references unknown flight {fid!r}")
            by_flight[fid].append(i)
    y_index = {fid: n_p + i for i, fid in enumerate(sorted(by_flight))}
    n_core = n_p + len(y_index)

    entries: list[tuple[int, int, float]] = []
    lbs: list[float] = []
    ubs: list[float] = []
    ground: list[tuple[int, str, str, object, object]] = []
    node_of: dict[tuple[str, str, object], int] = {}
    flight_arcs: list[tuple[str, str, int, int]] = []
    nrow = 0
    # y_f = sum of selected pairings containing f
    for fid, y in y_index.items():
        entries.append((nrow, y, 1.0))
        entries.extend((nrow, c, -1.0) for c in by_flight[fid])
        lbs.append(0.0)
        ubs.append(0.0)
        nrow += 1
    n_ground = 0
    served = set()
    for fam in families:
        tails = [a for a in aircraft if _u(a.equipment) == fam]
        fam_flights = [fid for fid in ids if _u(records[fid]["Eqp"]) == fam]
        times: dict[str, set] = collections.defaultdict(set)
        for fid in fam_flights:
            f = records[fid]
            times[_u(f["From"])].add(f["STD"])
            times[_u(f["To"])].add(f["STA"] + mgt[fid])
        for a in tails:
            times[_u(a.start_station)].add(a.available_at)
        last_nodes = set()
        for s, ts in times.items():
            ordered = sorted(ts)
            for t in ordered:
                node_of[(fam, s, t)] = nrow
                lbs.append(0.0)
                ubs.append(0.0)
                nrow += 1
            last_nodes.add(node_of[(fam, s, ordered[-1])])
        for a in tails:  # one unit of supply per tail at its start state
            r = node_of[(fam, _u(a.start_station), a.available_at)]
            lbs[r] -= 1.0
            ubs[r] -= 1.0
        for r in last_nodes:  # aircraft may end anywhere
            ubs[r] = np.inf
        for fid in fam_flights:
            if fid not in y_index:
                continue
            f = records[fid]
            d = node_of[(fam, _u(f["From"]), f["STD"])]
            r = node_of[(fam, _u(f["To"]), f["STA"] + mgt[fid])]
            entries += [(d, y_index[fid], -1.0), (r, y_index[fid], 1.0)]
            flight_arcs.append((fam, fid, d, r))
            served.add(fid)
        for s, ts in times.items():
            ordered = sorted(ts)
            for t0, t1 in zip(ordered, ordered[1:]):
                col = n_core + n_ground
                n_ground += 1
                entries += [(node_of[(fam, s, t0)], col, -1.0), (node_of[(fam, s, t1)], col, 1.0)]
                ground.append((col, fam, s, t0, t1))
    n = n_core + n_ground
    rr, cc, vv = zip(*entries)
    matrix = sp.csr_matrix((vv, (rr, cc)), shape=(nrow, n))
    upper = np.concatenate([np.ones(n_core), np.full(n_ground, np.inf)])
    for fid, y in y_index.items():
        if fid not in served:  # no aircraft of this equipment family exists
            upper[y] = 0.0
    integrality = np.zeros(n)
    integrality[:n_p] = 1
    base = LinearConstraint(matrix, np.array(lbs), np.array(ubs))

    cost1 = np.zeros(n)
    for y in y_index.values():
        cost1[y] = -1.0
    started = time.time()
    first = milp(cost1, constraints=base, integrality=integrality, bounds=Bounds(np.zeros(n), upper),
                 options={"time_limit": float(time_limit_seconds), "mip_rel_gap": 1e-9})
    if first.x is None:
        raise RuntimeError(f"Aircraft-flow solver found no feasible incumbent: {first.message}")
    solution = first.x
    count_proven = first.status == 0
    best_count = int(round(-float(np.dot(cost1, first.x))))
    message = str(first.message)
    secondary_proven = False
    if count_proven:
        y_row = sp.csr_matrix(([1.0] * len(y_index), ([0] * len(y_index), list(y_index.values()))), shape=(1, n))
        cost2 = np.zeros(n)
        for i, p in enumerate(columns):
            cost2[i] = float(p.cost)
        second = milp(cost2, constraints=[base, LinearConstraint(y_row, best_count, np.inf)],
                      integrality=integrality, bounds=Bounds(np.zeros(n), upper),
                      options={"time_limit": float(secondary_time_limit_seconds), "mip_rel_gap": 1e-6})
        if second.x is not None:
            solution = second.x
            secondary_proven = second.status == 0
            message = str(second.message) if secondary_proven else (
                "Maximum operated-flight count proven optimal; pairing cost not proven optimal: " + str(second.message))
        else:
            message = "Maximum operated-flight count proven optimal; cost pass returned no incumbent: " + str(second.message)
    else:
        message = "Feasible incumbent returned, but maximum operated-flight count is not proven optimal: " + str(first.message)

    chosen = [columns[i] for i in range(n_p) if solution[i] > 0.5]
    operated = {fid for p in chosen for fid in p.flight_ids}
    if sum(len(p.flight_ids) for p in chosen) != len(operated):
        raise RuntimeError("Internal solution audit: selected pairings overlap")

    # Decode tail routes from the flow, then audit them against the hard rules.
    remaining = {col: int(round(solution[col])) for col, *_ in ground}
    out_ground: dict[tuple[str, int], list[tuple[int, int]]] = collections.defaultdict(list)
    out_flight: dict[tuple[str, int], list[tuple[str, int]]] = collections.defaultdict(list)
    for col, fam, s, t0, t1 in ground:
        out_ground[(fam, node_of[(fam, s, t0)])].append((col, node_of[(fam, s, t1)]))
    for fam, fid, d, r in flight_arcs:
        if fid in operated:
            out_flight[(fam, d)].append((fid, r))
    aircraft_by_flight: dict[str, str] = {}
    start_cost = 0.0
    for a in aircraft:
        fam = _u(a.equipment)
        node = node_of[(fam, _u(a.start_station), a.available_at)]
        route: list[str] = []
        while True:
            if out_flight[(fam, node)]:
                fid, node = out_flight[(fam, node)].pop()
                route.append(fid)
                continue
            for col, nxt in out_ground[(fam, node)]:
                if remaining[col] > 0:
                    remaining[col] -= 1
                    node = nxt
                    break
            else:
                break
        route.sort(key=lambda fid: (records[fid]["STD"], fid))
        if route:
            start_cost += float(a.start_cost)
            first_leg = records[route[0]]
            if _u(first_leg["From"]) != _u(a.start_station) or first_leg["STD"] < a.available_at:
                raise RuntimeError(f"Internal solution audit: tail {a.tail_id!r} violates its start state")
        for prev, nxt_id in zip(route, route[1:]):
            p, c = records[prev], records[nxt_id]
            if _u(p["To"]) != _u(c["From"]) or c["STD"] < p["STA"] + mgt[prev]:
                raise RuntimeError(f"Internal solution audit: tail {a.tail_id!r} route breaks continuity/MGT")
        for fid in route:
            if _u(records[fid]["Eqp"]) != fam:
                raise RuntimeError(f"Internal solution audit: tail {a.tail_id!r} flies wrong equipment")
            if fid in aircraft_by_flight:
                raise RuntimeError(f"Internal solution audit: flight {fid!r} on multiple tails")
            aircraft_by_flight[fid] = a.tail_id
    if set(aircraft_by_flight) != operated:
        raise RuntimeError(
            "Internal solution audit: decoded aircraft routes differ from operated flights "
            f"(missing={sorted(operated - set(aircraft_by_flight))[:10]}, "
            f"extra={sorted(set(aircraft_by_flight) - operated)[:10]})"
        )

    cancelled = tuple(fid for fid in ids if fid not in operated)
    reasons = {fid: "NO_VALID_PAIRING_CANDIDATE" for fid in cancelled if fid not in by_flight}
    return JointRecoveryResult(
        cancelled_flights=cancelled,
        selected_pairing_ids=tuple(sorted(p.column_id for p in chosen)),
        aircraft_by_flight=aircraft_by_flight,
        cancellation_count=len(cancelled),
        secondary_objective=float(sum(float(p.cost) for p in chosen) + start_cost),
        solver_message=f"Aircraft-flow MILP ({time.time() - started:.1f}s): {message}",
        optimal_over_supplied_columns=bool(count_proven and secondary_proven),
        minimum_cancellation_count_proven=bool(count_proven),
        retiming_supported=False,
        cancellation_reasons=reasons,
    )