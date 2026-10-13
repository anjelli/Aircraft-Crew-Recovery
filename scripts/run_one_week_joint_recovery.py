#!/usr/bin/env python3
"""Run a full-equipment one-week joint crew/aircraft operate-cancel prototype.

This produces a review artifact even when inputs block optimization. A successful
prototype is optimal only over the independently validated pairing columns
already present in pairing_report.xlsx; retiming is not supported yet.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for p in (ROOT, SRC):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from column_generation import PairingColumn
from crew_recovery.aircraft_line_inputs import load_aircraft_line_inventory
from crew_recovery.independent_validator import (
    MGTResolver,
    Rules,
    load_fdp,
    pairing_groups,
    prepare_schedule,
    report_flight_id,
    check_pairing,
    validate_existing,
)
from crew_recovery.resource_constrained_pricing import ResourceConstrainedPairingPricer
from crew_recovery.joint_recovery import solve_joint_operate_cancel


def _canonical_schedule(path: Path) -> pd.DataFrame:
    frame = pd.read_excel(path)
    required = {"Day", "STD", "STA", "STD_sin", "STA_sin", "Flt Num", "From", "To", "Eqp", "Arvl Ctry"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"Canonical schedule is missing columns: {missing}")
    frame = frame.copy()
    for col in ("Day", "STD", "STA", "STD_sin", "STA_sin"):
        frame[col] = pd.to_datetime(frame[col], errors="coerce")
    if frame[["Day", "STD", "STA", "STD_sin", "STA_sin"]].isna().any(axis=None):
        raise ValueError("Canonical schedule contains invalid date/time values")
    return frame.drop_duplicates(
        subset=["Day", "STD", "STA", "Eqp", "Flt Num", "From", "To"]
    ).reset_index(drop=True)


def _derive_layover_stations(report: pd.DataFrame, hub: str = "SIN") -> list[str]:
    stations = set()
    for _pid, group, splits in pairing_groups(report):
        for previous, following in zip(splits, splits[1:]):
            if not previous or not following:
                continue
            station = str(group.iloc[previous[-1]]["To"]).strip().upper()
            next_origin = str(group.iloc[following[0]]["From"]).strip().upper()
            if station == next_origin and station and station != hub:
                stations.add(station)
    return sorted(stations)


def _independent_aircraft_check(decisions: pd.DataFrame, flights: pd.DataFrame, aircraft) -> list[dict]:
    violations = []
    resource = {a.tail_id: a for a in aircraft}
    lookup = {str(row["flight_id"]): row for row in flights.to_dict("records")}
    operated = decisions[decisions["recovery_action"] == "OPERATE"]
    for tail_id, group in operated.groupby("assigned_aircraft_tail"):
        tail = resource[tail_id]
        route = sorted((lookup[fid] for fid in group["flight_id"]), key=lambda f: (f["STD"], f["flight_id"]))
        if route and (
            route[0]["From"].strip().upper() != tail.start_station.strip().upper()
            or route[0]["STD"] < tail.available_at
        ):
            violations.append({"type": "AIRCRAFT_START_STATE", "tail_id": tail_id, "flight_id": route[0]["flight_id"]})
        for f in route:
            if str(f["Eqp"]).strip() != tail.equipment:
                violations.append({"type": "AIRCRAFT_EQUIPMENT", "tail_id": tail_id, "flight_id": f["flight_id"]})
        for previous, current in zip(route, route[1:]):
            gap = (current["STD"] - previous["STA"]).total_seconds() / 60
            mgt = float(previous["mgt_min"])
            if previous["To"].strip().upper() != current["From"].strip().upper() or gap < mgt - 1e-9:
                violations.append({
                    "type": "AIRCRAFT_CONNECTION", "tail_id": tail_id,
                    "flight_id": current["flight_id"], "gap_minutes": gap, "required_mgt": mgt,
                })
    return violations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-date", default="2026-10-25")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--time-limit-seconds", type=float, default=120.0)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output" / "one_week_joint_recovery")
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days must be positive")
    start = pd.Timestamp(args.start_date).normalize()
    end = start + pd.Timedelta(days=args.days - 1)
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)

    summary = {
        "scope": {"start_date": start.date().isoformat(), "end_date": end.date().isoformat(), "days": args.days},
        "status": "BLOCKED_NOT_OPTIMIZED",
        "equipment_in_scope": ["320", "32N", "32P", "32Q"],
        "retiming_supported": False,
        "aircraft_resources_are_provisional": True,
        "optimality_scope": "Only independently validated existing/generated pairing columns and generated aircraft route arcs.",
        "blockers": [],
    }
    try:
        raw_schedule = _canonical_schedule(ROOT / "schedule_kgp.xlsx")
        resolver = MGTResolver(ROOT / "MGT.xlsx")
        prepared = prepare_schedule(raw_schedule, resolver)
        active = prepared.loc[prepared["Day"].dt.normalize().between(start, end)].copy().reset_index(drop=True)
        if active.empty:
            raise ValueError("No active flights found for requested one-week window")
        if active["flight_id"].duplicated().any():
            raise ValueError("Active schedule contains duplicate flight IDs")
        bad_mgt_mask = active["mgt_min"].isna() | active["mgt_status"].astype(str).str.contains("_CONFLICT", regex=False)
        missing_mgt = active.loc[bad_mgt_mask, ["flight_id", "Flt Num", "From", "To", "Eqp", "STA_local", "mgt_status"]]
        if not missing_mgt.empty:
            missing_mgt.to_csv(out / "missing_mgt_rules.csv", index=False)
            summary["blockers"].append({
                "code": "MGT_RULE_MISSING",
                "count": int(len(missing_mgt)),
                "detail": "At least one active flight has missing or conflicting arrival-station/equipment MGT. The solver was not run with fabricated or ambiguous turnaround values.",
            })
            summary["active_flights"] = int(len(active))
            summary["missing_mgt_count"] = int(len(missing_mgt))
            (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
            return 0

        aircraft, line_assignments = load_aircraft_line_inventory(
            ROOT / "low_kgp", ROOT / "schedule_kgp.xlsx",
            start_date="2026-10-25", end_date="2027-03-27",
        )
        line_assignments.to_csv(out / "provisional_line_inventory.csv", index=False)

        report = pd.read_excel(ROOT / "pairing_report.xlsx")
        rules = Rules(fdp=load_fdp(ROOT / "fdp_rules.csv"), dsb=60, feb=0, deb=30, max_layover_h=72.0)
        statuses, violations, unmatched, skipped = validate_existing(
            report, prepared, rules, start=start, end=end
        )
        statuses.to_csv(out / "pairing_validation_status.csv", index=False)
        violations.to_csv(out / "pairing_validation_violations.csv", index=False)
        unmatched.to_csv(out / "pairing_validation_unmatched.csv", index=False)
        valid_ids = set(statuses.loc[statuses["Status"] == "VALID", "Pairing ID"].astype(str))
        candidate_columns = []
        candidate_rows = {}
        seen_sequences = set()
        for pid, group, splits in pairing_groups(report):
            if pid not in valid_ids:
                continue
            if not group["Flight Date"].between(start, end).all():
                continue
            flight_ids = tuple(report_flight_id(row) for _, row in group.iterrows())
            if not set(flight_ids).issubset(set(active["flight_id"])):
                continue
            if flight_ids in seen_sequences:
                continue
            col = PairingColumn(
                column_id=f"PAIR_{pid}",
                flight_ids=flight_ids,
                cost=1.0,
                pairing_type="independently_validated_existing_report",
                is_multi_day=len(splits) > 1,
                num_duties=len(splits),
            )
            candidate_columns.append(col)
            candidate_rows[col.column_id] = group.copy()
            seen_sequences.add(flight_ids)
        existing_valid_count = len(candidate_columns)

        # Generate additional legal pairing columns over the active week to
        # reduce dependence on the finite report pool. This is an explicit,
        # bounded domain: hub turns and two-duty hub/layover/hub pairings.
        def validate_generated(seq, splits, pairing_type):
            return check_pairing("generated-candidate", list(seq), splits, rules)

        pricer = ResourceConstrainedPairingPricer(
            active.to_dict("records"),
            fdp_limits=rules.fdp,
            duty_start_buffer=rules.dsb,
            fdp_end_buffer=rules.feb,
            duty_end_buffer=rules.deb,
            max_layover_hours=rules.max_layover_h,
            layover_stations=_derive_layover_stations(report, "SIN"),
            validator=validate_generated,
            pairing_cost=1.0,
            hub="SIN",
            max_sectors_per_duty=4,
        )
        candidate_degree = {str(fid): 0 for fid in active["flight_id"]}
        for col in candidate_columns:
            for fid in col.flight_ids:
                candidate_degree[fid] = candidate_degree.get(fid, 0) + 1
        # Price uncovered/fragile flights first; this keeps the generated pool
        # focused instead of materializing every legal pairing in the week.
        uncovered_before_pricing = sum(degree == 0 for degree in candidate_degree.values())
        single_option_before_pricing = sum(degree == 1 for degree in candidate_degree.values())
        dual_prices = {
            fid: (1000.0 if degree == 0 else 0.1 if degree == 1 else 0.0)
            for fid, degree in candidate_degree.items()
        }
        priced_batch = pricer(
            dual_prices=dual_prices,
            active_column_ids=set(),
            reduced_cost_tolerance=0.0,
            batch_size=20000,
        )
        generated_count = 0
        duplicate_generated = 0
        for col in priced_batch.columns:
            if col.flight_ids in seen_sequences:
                duplicate_generated += 1
                continue
            if not set(col.flight_ids).issubset(set(active["flight_id"])):
                continue
            candidate_columns.append(col)
            seen_sequences.add(col.flight_ids)
            generated_count += 1

        mgt_flights = active.rename(columns={"Eqp": "Eqp"})[
            ["flight_id", "From", "To", "STD", "STA", "Eqp", "mgt_min"]
        ].to_dict("records")
        result = solve_joint_operate_cancel(
            mgt_flights, candidate_columns, aircraft,
            time_limit_seconds=args.time_limit_seconds,
        )
        selected_pairings = set(result.selected_pairing_ids)
        selected_map = {}
        for pid in selected_pairings:
            for fid in next(c.flight_ids for c in candidate_columns if c.column_id == pid):
                if fid in selected_map:
                    raise RuntimeError(f"Two selected pairings cover flight {fid!r}")
                selected_map[fid] = pid
        cancelled = set(result.cancelled_flights)
        decision_rows = []
        active_records = active.set_index("flight_id").to_dict("index")
        for fid in active["flight_id"]:
            is_cancelled = fid in cancelled
            decision_rows.append({
                "flight_id": fid,
                "service_date": pd.Timestamp(active_records[fid]["Day"]).date().isoformat(),
                "flight_number": int(active_records[fid]["Flt Num"]),
                "From": active_records[fid]["From"],
                "To": active_records[fid]["To"],
                "Eqp": active_records[fid]["Eqp"],
                "original_STD_local": str(active_records[fid]["STD_local"]),
                "original_STA_local": str(active_records[fid]["STA_local"]),
                "recovered_STD_local": None if is_cancelled else str(active_records[fid]["STD_local"]),
                "recovered_STA_local": None if is_cancelled else str(active_records[fid]["STA_local"]),
                "recovery_action": "CANCEL" if is_cancelled else "OPERATE",
                "cancellation_reason": "Selected by joint operate/cancel optimum; inspect pairing and aircraft feasibility audits." if is_cancelled else None,
                "assigned_pairing_id": None if is_cancelled else selected_map.get(fid),
                "assigned_aircraft_tail": None if is_cancelled else result.aircraft_by_flight.get(fid),
                "retime_minutes": 0,
            })
        decisions = pd.DataFrame(decision_rows)
        selected_pairing_rows = []
        for pid in sorted(selected_pairings):
            col = next(c for c in candidate_columns if c.column_id == pid)
            for seq, fid in enumerate(col.flight_ids, start=1):
                selected_pairing_rows.append({"pairing_id": pid, "sequence": seq, "flight_id": fid})
        pairing_output = pd.DataFrame(selected_pairing_rows)
        rotations = decisions.loc[decisions["recovery_action"] == "OPERATE"].merge(
            active[["flight_id", "STD", "STA", "mgt_min"]], on="flight_id", how="left"
        ).sort_values(["assigned_aircraft_tail", "STD", "flight_id"])
        aircraft_violations = _independent_aircraft_check(
            decisions, active[["flight_id", "From", "To", "STD", "STA", "Eqp", "mgt_min"]], aircraft
        )
        crew_coverage_errors = []
        counts = decisions.loc[decisions["recovery_action"] == "OPERATE", "flight_id"].map(
            lambda fid: int(fid in selected_map)
        )
        if len(counts) and not counts.eq(1).all():
            crew_coverage_errors.append("An operated flight does not have exactly one selected pairing")
        if set(decisions.loc[decisions["recovery_action"] == "OPERATE", "flight_id"]) != set(result.aircraft_by_flight):
            crew_coverage_errors.append("Operated flight set differs from aircraft-assigned flight set")
        if aircraft_violations or crew_coverage_errors:
            summary["blockers"].append({
                "code": "POST_SOLVE_VALIDATION_FAILED",
                "aircraft_violations": aircraft_violations,
                "crew_coverage_errors": crew_coverage_errors,
            })
            summary["status"] = "BLOCKED_POST_SOLVE_VALIDATION_FAILED"
        elif result.optimal_over_supplied_columns:
            summary["status"] = "PROTOTYPE_OPTIMAL_OVER_SUPPLIED_COLUMNS"
        else:
            summary["status"] = "FEASIBLE_NOT_PROVEN_OPTIMAL"
        summary.update({
            "active_flights": int(len(active)),
            "existing_valid_pairing_candidates": int(existing_valid_count),
            "flights_without_existing_pairing_candidate": int(uncovered_before_pricing),
            "flights_with_one_existing_pairing_candidate": int(single_option_before_pricing),
            "generated_pairing_candidates": int(generated_count),
            "pricing_explored_states": int(priced_batch.explored_states),
            "pricing_domain_status": str(priced_batch.status),
            "pricing_best_reduced_cost": priced_batch.best_reduced_cost,
            "duplicate_generated_pairings_skipped": int(duplicate_generated),
            "validated_pairing_candidates": int(len(candidate_columns)),
            "invalid_pairing_candidates_in_status_report": int((statuses["Status"] == "INVALID").sum()),
            "pairings_skipped_across_window_boundary": int(skipped),
            "selected_pairings": int(len(selected_pairings)),
            "operated_flights": int((decisions["recovery_action"] == "OPERATE").sum()),
            "cancelled_flights": int(len(cancelled)),
            "aircraft_resources": int(len(aircraft)),
            "aircraft_violations": aircraft_violations,
            "crew_coverage_errors": crew_coverage_errors,
            "solver_message": result.solver_message,
            "secondary_objective": result.secondary_objective,
            "minimum_cancellation_count_proven": result.minimum_cancellation_count_proven,
            "full_lexicographic_optimum_proven": result.optimal_over_supplied_columns,
            "assumptions": [
                "Aircraft LINE_n identifiers and starting states are inferred from AIRFLITE lines of work, not verified tail registrations.",
                "MGTResolver uses the validator's documented demo aircraft-group mapping; owner-approved equipment-group compatibility remains required.",
                "No retiming or flight-copy generation is supported in this prototype.",
                "Optimality is restricted to existing pairing-report columns independently marked VALID and the generated aircraft route arcs.",
            ],
        })
        decisions.to_csv(out / "flight_decisions.csv", index=False)
        pairing_output.to_csv(out / "selected_pairings.csv", index=False)
        rotations.to_csv(out / "aircraft_rotations.csv", index=False)
        pd.DataFrame(aircraft_violations).to_csv(out / "aircraft_validation_violations.csv", index=False)
        (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        with pd.ExcelWriter(out / "one_week_joint_recovery.xlsx", engine="openpyxl") as writer:
            decisions.to_excel(writer, sheet_name="Flight Decisions", index=False)
            pairing_output.to_excel(writer, sheet_name="Selected Pairings", index=False)
            rotations.to_excel(writer, sheet_name="Aircraft Rotations", index=False)
            statuses.to_excel(writer, sheet_name="Pairing Validation", index=False)
            pd.DataFrame(aircraft_violations).to_excel(writer, sheet_name="Aircraft Validation", index=False)
            pd.DataFrame([summary]).to_excel(writer, sheet_name="Summary", index=False)
    except Exception as exc:
        summary["blockers"].append({"code": "RUNNER_EXCEPTION", "detail": f"{type(exc).__name__}: {exc}"})
        (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
