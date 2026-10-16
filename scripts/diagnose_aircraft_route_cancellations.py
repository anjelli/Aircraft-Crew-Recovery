#!/usr/bin/env python3
"""Diagnose why the large-horizon aircraft-route heuristic cancels flights.

Re-runs the real runner pipeline (so the inputs are exactly what the heuristic
sees), captures the heuristic's inputs, and then reports:

1. an exact replay of the best heuristic plan, with a leg-level classification
   of why each rejected pairing could not be inserted into an aircraft route;
2. a proven-optimal bound from a time-expanded aircraft-flow MILP that uses the
   SAME hard rules (equipment match, station continuity, MGT, tail start state,
   no repositioning, all-or-nothing crew pairings);
3. a decomposition of the cancellations into: no-crew-candidate flights,
   aircraft-only knock-on, pairing-coupling knock-on, and heuristic loss.

Nothing here changes the runner's results; it is a read-only diagnostic.
Usage:  python scripts/diagnose_aircraft_route_cancellations.py [--days 154] [--pairing-mode existing-only|expanded]
"""
from __future__ import annotations

import argparse
import collections
import json
import runpy
import sys
import time
from bisect import bisect_left
from datetime import timedelta
from pathlib import Path

import numpy as np
import scipy.sparse as sp
from scipy.optimize import Bounds, LinearConstraint, milp

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "src", ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from column_generation import PairingColumn  # noqa: E402

U = lambda s: str(s).strip().upper()  # noqa: E731


# --------------------------------------------------------------------------- capture
def capture_solver_inputs(days: int, pairing_mode: str, scratch: Path, scenario_file: Path | None = None):
    import crew_recovery.joint_recovery as jr

    captured: dict[str, object] = {}
    original = jr.solve_joint_operate_cancel_greedy

    def spy(flights, pairings, aircraft, **kwargs):
        captured.update(flights=list(flights), pairings=list(pairings),
                        aircraft=list(aircraft), kwargs=dict(kwargs))
        result = original(flights, pairings, aircraft, **kwargs)
        captured["result"] = result
        return result

    jr.solve_joint_operate_cancel_greedy = spy
    argv = ["run_one_week_joint_recovery.py", "--days", str(days), "--output-dir", str(scratch)]
    if pairing_mode == "existing-only":
        argv.append("--skip-pricing")
    if scenario_file is not None:
        argv += ["--scenario-file", str(scenario_file)]
    old_argv = sys.argv
    sys.argv = argv
    try:
        runpy.run_path(str(ROOT / "scripts" / "run_one_week_joint_recovery.py"), run_name="__main__")
    except SystemExit:
        pass
    finally:
        sys.argv = old_argv
        jr.solve_joint_operate_cancel_greedy = original
    if "flights" not in captured:
        raise RuntimeError("Runner did not call the greedy heuristic (horizon <= 1500 flights uses the MILP).")
    return captured


# --------------------------------------------------------------------------- replay
def _can_follow(a, b):
    return U(a["To"]) == U(b["From"]) and b["STD"] >= a["STA"] + timedelta(minutes=float(a["mgt_min"]))


def _why_not(route, f, tail):
    """None when `f` can be inserted into `route` of `tail`; otherwise a reason code."""
    if str(f["Eqp"]).strip() != tail.equipment.strip():
        return "EQUIPMENT_MISMATCH"
    pos = bisect_left(route, (f["STD"], f["flight_id"]), key=lambda i: (i["STD"], i["flight_id"]))
    if pos == 0:
        if U(f["From"]) != U(tail.start_station):
            return "FIRST_LEG_NOT_AT_TAIL_START_STATION"
        if f["STD"] < tail.available_at:
            return "BEFORE_TAIL_AVAILABLE"
    else:
        prev = route[pos - 1]
        if U(prev["To"]) != U(f["From"]):
            return "NO_TAIL_AT_ORIGIN_STATION"
        if not _can_follow(prev, f):
            return "PREDECESSOR_MGT_TOO_TIGHT"
    if pos < len(route):
        nxt = route[pos]
        if U(f["To"]) != U(nxt["From"]):
            return "SUCCESSOR_STATION_MISMATCH"
        if not _can_follow(f, nxt):
            return "SUCCESSOR_MGT_TOO_TIGHT"
    return None


def replay_best_plan(captured):
    """Exact replay of the free / chronological-first retry plan with leg-level reasons."""
    records = {f["flight_id"]: f for f in captured["flights"]}
    tails = captured["aircraft"]
    pf = {p.column_id: tuple(p.flight_ids) for p in captured["pairings"]}
    start = lambda p: min(records[f]["STD"] for f in pf[p.column_id])  # noqa: E731
    per_flight = lambda p: float(p.cost) / max(1, len(pf[p.column_id]))  # noqa: E731
    order = sorted(captured["pairings"], key=lambda p: (start(p), -len(pf[p.column_id]), per_flight(p), p.column_id))
    routes = {t.tail_id: [] for t in tails}
    covered, pending = set(), list(order)
    for _ in range(5):
        nxt, progress = [], False
        for p in pending:
            fids = pf[p.column_id]
            if any(f in covered for f in fids):
                continue
            undo, ok = [], True
            for f in sorted((records[x] for x in fids), key=lambda f: (f["STD"], f["flight_id"])):
                best = None
                for t in tails:
                    if _why_not(routes[t.tail_id], f, t) is not None:
                        continue
                    route = routes[t.tail_id]
                    pos = bisect_left(route, (f["STD"], f["flight_id"]), key=lambda i: (i["STD"], i["flight_id"]))
                    gap = (f["STD"] - route[pos - 1]["STA"]).total_seconds() if pos > 0 else 0.0
                    gap += (route[pos]["STD"] - f["STA"]).total_seconds() if pos < len(route) else 0.0
                    key = (0, gap, len(route), t.tail_id)
                    if best is None or key < best[0]:
                        best = (key, t.tail_id, pos)
                if best is None:
                    ok = False
                    break
                routes[best[1]].insert(best[2], f)
                undo.append((best[1], best[2]))
            if not ok:
                for tid, pos in reversed(undo):
                    routes[tid].pop(pos)
                nxt.append(p)
                continue
            covered.update(fids)
            progress = True
        pending = nxt
        if not pending or not progress:
            break

    reasons, first_leg = collections.Counter(), collections.Counter()
    for p in pending:
        for idx, fid in enumerate(sorted(pf[p.column_id], key=lambda x: (records[x]["STD"], x))):
            counts = collections.Counter()
            insertable = False
            for t in tails:
                why = _why_not(routes[t.tail_id], records[fid], t)
                if why is None:
                    insertable = True
                    break
                if why != "EQUIPMENT_MISMATCH":
                    counts[why] += 1
            if not insertable:
                first_leg[idx] += 1
                reasons[counts.most_common(1)[0][0] if counts else "NO_TAIL_OF_REQUIRED_EQUIPMENT"] += 1
                break
    return {
        "replayed_operated_flights": len(covered),
        "rejected_pairings_at_end": len(pending),
        "index_of_first_uninsertable_leg": dict(first_leg),
        "dominant_blocking_reason_per_rejected_pairing": dict(reasons),
    }, covered


# --------------------------------------------------------------------------- flow bound
def solve_flow_bound(captured, mode: str, time_limit: float = 600.0):
    """Max operated flights under the heuristic's hard rules, as a time-expanded flow MILP.

    mode: 'crew_pairings'            all-or-nothing validated pairings (the real model)
          'uncrewed_flights_operable' as above, but flights with no pairing may also operate
          'flight_level'             any flight that appears in some pairing may operate alone
    """
    flights, aircraft = captured["flights"], captured["aircraft"]
    pairings = list(captured["pairings"])
    records = {f["flight_id"]: f for f in flights}
    covered = {f for p in pairings for f in p.flight_ids}
    single = lambda f: PairingColumn(column_id="S_" + f, flight_ids=(f,), cost=0.0)  # noqa: E731
    if mode == "uncrewed_flights_operable":
        pairings += [single(f) for f in records if f not in covered]
    elif mode == "flight_level":
        pairings = [single(f) for f in covered]
    elif mode != "crew_pairings":
        raise ValueError(mode)

    pidx = {p.column_id: i for i, p in enumerate(pairings)}
    n_p = len(pairings)
    by_flight = collections.defaultdict(list)
    for p in pairings:
        for f in p.flight_ids:
            by_flight[f].append(pidx[p.column_id])
    y_index = {f: n_p + i for i, f in enumerate(sorted(by_flight))}
    n_core = n_p + len(y_index)

    entries, lbs, ubs, ground = [], [], [], []
    nrow = 0
    # y_f = sum_{p contains f} x_p
    for f, y in y_index.items():
        entries.append((nrow, y, 1.0))
        entries.extend((nrow, c, -1.0) for c in by_flight[f])
        lbs.append(0.0)
        ubs.append(0.0)
        nrow += 1
    node_of, flight_arcs = {}, []
    ng = 0
    for fam in sorted({U(a.equipment) for a in aircraft}):
        tl = [a for a in aircraft if U(a.equipment) == fam]
        ff = [f for f in flights if U(f["Eqp"]) == fam]
        times = collections.defaultdict(set)
        for f in ff:
            times[U(f["From"])].add(f["STD"])
            times[U(f["To"])].add(f["STA"] + timedelta(minutes=float(f["mgt_min"])))
        for a in tl:
            times[U(a.start_station)].add(a.available_at)
        last = set()
        for s, ts in times.items():
            ts = sorted(ts)
            for t in ts:
                node_of[(fam, s, t)] = nrow
                lbs.append(0.0)
                ubs.append(0.0)
                nrow += 1
            last.add(node_of[(fam, s, ts[-1])])
        for a in tl:  # one unit of supply per tail at its start state
            r = node_of[(fam, U(a.start_station), a.available_at)]
            lbs[r] -= 1.0
            ubs[r] -= 1.0
        for r in last:  # aircraft may end anywhere
            ubs[r] = np.inf
        for f in ff:
            if f["flight_id"] not in y_index:
                continue
            d = node_of[(fam, U(f["From"]), f["STD"])]
            r = node_of[(fam, U(f["To"]), f["STA"] + timedelta(minutes=float(f["mgt_min"])))]
            y = y_index[f["flight_id"]]
            entries += [(d, y, -1.0), (r, y, 1.0)]
            flight_arcs.append((fam, f["flight_id"], d, r))
        for s, ts in times.items():
            ts = sorted(ts)
            for t0, t1 in zip(ts, ts[1:]):
                col = n_core + ng
                ng += 1
                entries += [(node_of[(fam, s, t0)], col, -1.0), (node_of[(fam, s, t1)], col, 1.0)]
                ground.append((col, fam, s, t0, t1))
    n = n_core + ng
    rr, cc, vv = zip(*entries)
    A = sp.csr_matrix((vv, (rr, cc)), shape=(nrow, n))
    cost = np.zeros(n)
    for f, y in y_index.items():
        cost[y] = -1.0
    integrality = np.zeros(n)
    integrality[:n_p] = 1
    upper = np.concatenate([np.ones(n_core), np.full(ng, np.inf)])
    t_start = time.time()
    res = milp(cost, constraints=LinearConstraint(A, np.array(lbs), np.array(ubs)),
               integrality=integrality, bounds=Bounds(np.zeros(n), upper),
               options={"time_limit": time_limit, "mip_rel_gap": 1e-6})
    out = {"mode": mode, "solver_status": int(res.status), "solver_message": str(res.message),
           "solve_seconds": round(time.time() - t_start, 2), "pairing_columns": n_p, "model_rows": nrow, "model_cols": n}
    if res.x is None:
        return out, None
    x = res.x
    chosen = [pairings[i] for i in range(n_p) if x[i] > 0.5]
    operated = {f for p in chosen for f in p.flight_ids}
    out.update(operated_flights=len(operated), selected_pairings=len(chosen),
               proven_optimal=bool(res.status == 0))
    if mode == "crew_pairings":
        out["independent_route_violations"] = _decode_and_validate(
            x, ground, flight_arcs, node_of, operated, aircraft, records)
    return out, operated


def _decode_and_validate(x, ground, flight_arcs, node_of, operated, aircraft, records):
    rem = {col: int(round(x[col])) for col, *_ in ground}
    out_g, out_f = collections.defaultdict(list), collections.defaultdict(list)
    for col, fam, s, t0, t1 in ground:
        out_g[(fam, node_of[(fam, s, t0)])].append((col, node_of[(fam, s, t1)]))
    for fam, fid, d, r in flight_arcs:
        if fid in operated:
            out_f[(fam, d)].append((fid, r))
    seen, viol = set(), collections.Counter()
    for a in aircraft:
        fam = U(a.equipment)
        node, route = node_of[(fam, U(a.start_station), a.available_at)], []
        while True:
            if out_f[(fam, node)]:
                fid, node = out_f[(fam, node)].pop()
                route.append(records[fid])
                continue
            for col, nxt in out_g[(fam, node)]:
                if rem[col] > 0:
                    rem[col] -= 1
                    node = nxt
                    break
            else:
                break
        route.sort(key=lambda f: (f["STD"], f["flight_id"]))
        for f in route:
            seen.add(f["flight_id"])
            if U(f["Eqp"]) != U(a.equipment):
                viol["equipment"] += 1
        if route and (U(route[0]["From"]) != U(a.start_station) or route[0]["STD"] < a.available_at):
            viol["start_state"] += 1
        for p, c in zip(route, route[1:]):
            if U(p["To"]) != U(c["From"]):
                viol["station_continuity"] += 1
            elif c["STD"] < p["STA"] + timedelta(minutes=float(p["mgt_min"])):
                viol["mgt"] += 1
    if seen != operated:
        viol["decoded_flight_set_mismatch"] += abs(len(seen ^ operated))
    return dict(viol)


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=154)
    ap.add_argument("--pairing-mode", choices=("existing-only", "expanded"), default="existing-only")
    ap.add_argument("--output-dir", type=Path, default=ROOT / "output" / "audit" / "aircraft_route_cancellation_diagnosis")
    ap.add_argument("--scenario-file", type=Path, default=None,
                    help="Optional scenario/trial manifest, applied exactly as the runner applies it.")
    ap.add_argument("--watch-flights", default="485_20261025T132000,486_20261025T154500,487_20261025T175000",
                    help="Comma-separated flight_ids whose outcome is reported for heuristic and flow optimum.")
    ap.add_argument("--flow-time-limit-seconds", type=float, default=600.0)
    args = ap.parse_args()
    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)

    cap = capture_solver_inputs(args.days, args.pairing_mode, out / "_runner_scratch", args.scenario_file)
    records = {f["flight_id"]: f for f in cap["flights"]}
    has_pairing = {f for p in cap["pairings"] for f in p.flight_ids}
    heuristic = cap["result"]
    report = {
        "days": args.days, "pairing_mode": args.pairing_mode,
        "eligible_flights": len(records), "pairing_columns": len(cap["pairings"]),
        "aircraft_resources": len(cap["aircraft"]),
        "flights_with_no_pairing_candidate": len(records) - len(has_pairing),
        "heuristic_operated": len(records) - heuristic.cancellation_count,
        "heuristic_reason_counts": dict(collections.Counter(heuristic.cancellation_reasons.values())),
    }
    replay, covered = replay_best_plan(cap)
    report["heuristic_replay"] = replay
    report["heuristic_replay_matches_runner"] = (len(covered) == report["heuristic_operated"])

    bounds = {}
    for mode in ("crew_pairings", "uncrewed_flights_operable", "flight_level"):
        info, operated = solve_flow_bound(cap, mode, args.flow_time_limit_seconds)
        bounds[mode] = info
        if mode == "crew_pairings" and operated is not None:
            watch = [w for w in args.watch_flights.split(",") if w in records]
            report["watched_flights"] = {
                w: {"heuristic": "OPERATE" if w not in set(heuristic.cancelled_flights) else "CANCEL",
                    "flow_optimum": "OPERATE" if w in operated else "CANCEL"} for w in watch}
    report["flow_bounds"] = bounds
    n, op_h = len(records), report["heuristic_operated"]
    op_pair = bounds["crew_pairings"].get("operated_flights")
    op_flt = bounds["flight_level"].get("operated_flights")
    no_pair = report["flights_with_no_pairing_candidate"]
    if op_pair is not None and op_flt is not None:
        report["decomposition_of_cancellations"] = {
            "total_cancelled": n - op_h,
            "no_crew_candidate_flights": no_pair,
            "aircraft_only_knock_on": (n - no_pair) - op_flt,
            "pairing_coupling_knock_on": op_flt - op_pair,
            "heuristic_loss_vs_proven_optimum": op_pair - op_h,
            "check_sum_equals_total": no_pair + ((n - no_pair) - op_flt) + (op_flt - op_pair) + (op_pair - op_h) == n - op_h,
        }
    (out / "diagnosis.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())