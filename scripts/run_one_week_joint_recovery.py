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
from crew_recovery.aircraft_inventory import load_registered_aircraft_inventory
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
from crew_recovery.crew_costing import crew_salary_cost, generated_pairing_cost_callback
from crew_recovery.pairing_scoring import pairing_efficiency_score
from crew_recovery.operational_objective import objective_contract
from crew_recovery.objective_spec import load_cost_parameters
from crew_recovery.joint_recovery import solve_joint_operate_cancel, solve_joint_operate_cancel_greedy
from crew_recovery.project_paths import input_file, original_file


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
    parser.add_argument(
        "--skip-pricing", action="store_true",
        help="Use only independently valid existing pairing-report columns; useful for bounded horizon-scale smoke runs.",
    )
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
        "objective_policy": ["minimize_cancellations", "minimize_midnight_aware_crew_salary_plus_aircraft_start_cost"],
        "objective_contract": objective_contract(retiming_integrated=False, crew_roster_integrated=False),
        "objective_cost_basis": "Crew salary uses cost_parameters.csv man_day_cost multiplied by each duty calendar day touched, including the second day when duty crosses midnight; other allowances and retiming are not yet integrated.",
        "candidate_ranking": "Salary cost per covered sector, then duty count; ordering only, every legal generated column is retained. This is a heuristic baseline, not trained ML.",
        "aircraft_resources_are_provisional": True,
        "registered_tail_inventory_loaded": False,
        "operational_acceptance": False,
        "optimality_scope": "Only independently validated existing/generated pairing columns and generated aircraft route arcs.",
        "blockers": [],
    }
    try:
        raw_schedule = _canonical_schedule(input_file("schedule.xlsx"))
        resolver = MGTResolver(original_file("MGT.xlsx"))
        prepared = prepare_schedule(raw_schedule, resolver)
        active = prepared.loc[prepared["Day"].dt.normalize().between(start, end)].copy().reset_index(drop=True)
        if active.empty:
            raise ValueError("No active flights found for requested one-week window")
        if active["flight_id"].duplicated().any():
            raise ValueError("Active schedule contains duplicate flight IDs")
        all_active = active.copy()
        bad_mgt_mask = active["mgt_min"].isna() | active["mgt_status"].astype(str).str.contains("_CONFLICT|MISSING|OUT_OF_EFFECTIVE_DATE", regex=True)
        missing_mgt = active.loc[bad_mgt_mask, ["flight_id", "Flt Num", "From", "To", "Eqp", "STA_local", "mgt_status"]].copy()
        unresolved_mgt_ids = set(missing_mgt["flight_id"].astype(str))
        if not missing_mgt.empty:
            missing_mgt.to_csv(out / "missing_mgt_rules.csv", index=False)
            summary["blockers"].append({
                "code": "MGT_RULE_MISSING",
                "count": int(len(missing_mgt)),
                "detail": "Flights with missing/conflicting/out-of-effective-date MGT are excluded from optimization and explicitly exported as UNRESOLVED_MGT. No turnaround values are fabricated.",
            })
        # Optimize only flights with a resolvable, unambiguous MGT. Keep unresolved
        # flights in the final flight-level file rather than blocking all other dates.
        active = active.loc[~bad_mgt_mask].copy().reset_index(drop=True)
        if active.empty:
            summary["status"] = "BLOCKED_NO_MGT_RESOLVED_FLIGHTS"
            summary["active_flights"] = int(len(all_active))
            summary["optimization_eligible_flights"] = 0
            summary["unresolved_mgt_flights"] = int(len(missing_mgt))
            summary["missing_mgt_count"] = int(len(missing_mgt))
            missing_mgt.to_csv(out / "flight_decisions.csv", index=False)
            (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
            return 0

        provisional_aircraft, line_assignments = load_aircraft_line_inventory(
            original_file("low_kgp"), input_file("schedule.xlsx"),
            start_date="2026-10-25", end_date="2027-03-27",
        )
        line_assignments.to_csv(out / "provisional_line_inventory.csv", index=False)
        registered_inventory_path = ROOT / "aircraft_inventory.csv"
        if registered_inventory_path.exists():
            aircraft = load_registered_aircraft_inventory(registered_inventory_path)
            summary["aircraft_resources_are_provisional"] = False
            summary["registered_tail_inventory_loaded"] = True
        else:
            aircraft = provisional_aircraft
            summary["blockers"].append({
                "code": "VERIFIED_TAIL_INVENTORY_MISSING",
                "detail": "No aircraft_inventory.csv found. LINE_n resources are only provisional line-of-work proxies; the solve is a research baseline, not an operational tail assignment.",
            })

        report = pd.read_excel(input_file("pairing_report.xlsx"))
        costs = load_cost_parameters(original_file("cost_parameters.csv"))
        man_day_cost = costs["man_day_cost"]
        rules = Rules(fdp=load_fdp(original_file("fdp_rules.csv")), dsb=60, feb=0, deb=30, max_layover_h=72.0)
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
        active_record_lookup = {
            str(row["flight_id"]): row for row in active.to_dict("records")
        }
        for pid, group, splits in pairing_groups(report):
            if pid not in valid_ids:
                continue
            if not group["Flight Date"].between(start, end).all():
                continue
            flight_ids = tuple(report_flight_id(row) for _, row in group.iterrows())
            if not set(flight_ids).issubset(set(active_record_lookup)):
                continue
            if flight_ids in seen_sequences:
                continue
            # Cost the pairing against canonical, validated schedule records, not
            # raw report rows. The report may contain blank/malformed local-time
            # fields and does not carry the canonical flight_id used by the solver.
            salary_sequence = [active_record_lookup[fid] for fid in flight_ids]
            col = PairingColumn(
                column_id=f"PAIR_{pid}",
                flight_ids=flight_ids,
                cost=crew_salary_cost(
                    salary_sequence, splits, man_day_cost=man_day_cost,
                    duty_start_buffer_minutes=rules.dsb, duty_end_buffer_minutes=rules.deb,
                ),
                pairing_type="independently_validated_existing_report",
                is_multi_day=len(splits) > 1,
                num_duties=len(splits),
            )
            candidate_columns.append(col)
            candidate_rows[col.column_id] = group.copy()
            seen_sequences.add(flight_ids)
        existing_valid_count = len(candidate_columns)

        candidate_degree = {str(fid): 0 for fid in active["flight_id"]}
        for col in candidate_columns:
            for fid in col.flight_ids:
                candidate_degree[fid] = candidate_degree.get(fid, 0) + 1
        uncovered_flights = {fid for fid, degree in candidate_degree.items() if degree == 0}

        # Generate additional legal pairing columns over the active week to
        # reduce dependence on the finite report pool. One-/two-duty options
        # are enumerated in the declared domain; three-duty options are focused
        # on flights that currently have no valid pairing candidate.
        def validate_generated(seq, splits, pairing_type):
            return check_pairing("generated-candidate", list(seq), splits, rules)

        # Record the pre-generation coverage gaps so the candidate expansion is auditable.
        uncovered_before_pricing = sum(degree == 0 for degree in candidate_degree.values())
        single_option_before_pricing = sum(degree == 1 for degree in candidate_degree.values())
        generated_count = 0
        duplicate_generated = 0
        generated_domain_size = 0
        pricing_states_explored = 0
        if not args.skip_pricing:
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
                pairing_cost_fn=generated_pairing_cost_callback(
                    man_day_cost=man_day_cost,
                    duty_start_buffer_minutes=rules.dsb,
                    duty_end_buffer_minutes=rules.deb,
                ),
                candidate_score_fn=pairing_efficiency_score,
                hub="SIN",
                max_sectors_per_duty=4,
                max_duties=3,
                focus_flight_ids=uncovered_flights,
            )
            generated_pool = pricer.generate_candidates()
            generated_domain_size = len(generated_pool)
            pricing_states_explored = pricer.states_explored
            active_ids = set(active["flight_id"])
            for col in generated_pool:
                if col.flight_ids in seen_sequences:
                    duplicate_generated += 1
                    continue
                if not set(col.flight_ids).issubset(active_ids):
                    continue
                candidate_columns.append(col)
                seen_sequences.add(col.flight_ids)
                generated_count += 1

        mgt_flights = active.rename(columns={"Eqp": "Eqp"})[
            ["flight_id", "From", "To", "STD", "STA", "Eqp", "mgt_min"]
        ].to_dict("records")
        # The full MILP is appropriate for the small baseline. At month/full scale,
        # the route-flow binary model can spend its time limit before finding an
        # incumbent; use the explicit deterministic feasibility heuristic instead.
        solver_mode = "joint_milp"
        if len(active) > 1500:
            result = solve_joint_operate_cancel_greedy(mgt_flights, candidate_columns, aircraft)
            solver_mode = "deterministic_greedy_large_horizon"
        else:
            try:
                result = solve_joint_operate_cancel(
                    mgt_flights, candidate_columns, aircraft,
                    time_limit_seconds=args.time_limit_seconds,
                )
            except RuntimeError as solve_error:
                if "no feasible incumbent" not in str(solve_error).lower():
                    raise
                result = solve_joint_operate_cancel_greedy(mgt_flights, candidate_columns, aircraft)
                solver_mode = "deterministic_greedy_no_incumbent_fallback"
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
                "cancellation_reason": ("Not selected by the deterministic heuristic from the supplied validated pairing pool; not proof of operational infeasibility." if solver_mode.startswith("deterministic_greedy") else "Selected by the joint operate/cancel MILP; inspect pairing and aircraft feasibility audits.") if is_cancelled else None,
                "assigned_pairing_id": None if is_cancelled else selected_map.get(fid),
                "assigned_aircraft_tail": None if is_cancelled else result.aircraft_by_flight.get(fid),
                "retime_minutes": 0,
            })
        decisions = pd.DataFrame(decision_rows)
        if not missing_mgt.empty:
            unresolved_rows = []
            all_records = all_active.set_index("flight_id").to_dict("index")
            for fid in sorted(unresolved_mgt_ids):
                row = all_records[fid]
                unresolved_rows.append({
                    "flight_id": fid,
                    "service_date": pd.Timestamp(row["Day"]).date().isoformat(),
                    "flight_number": int(row["Flt Num"]),
                    "From": row["From"], "To": row["To"], "Eqp": row["Eqp"],
                    "original_STD_local": str(row["STD_local"]),
                    "original_STA_local": str(row["STA_local"]),
                    "recovered_STD_local": None, "recovered_STA_local": None,
                    "recovery_action": "UNRESOLVED_MGT",
                    "cancellation_reason": "No unambiguous applicable MGT rule; requires data-owner resolution.",
                    "assigned_pairing_id": None, "assigned_aircraft_tail": None,
                    "retime_minutes": None,
                })
            decisions = pd.concat([decisions, pd.DataFrame(unresolved_rows)], ignore_index=True)
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
        if not summary["registered_tail_inventory_loaded"] and summary["status"] == "PROTOTYPE_OPTIMAL_OVER_SUPPLIED_COLUMNS":
            summary["status"] = "PROTOTYPE_OPTIMAL_WITH_PROVISIONAL_TAILS"
        if solver_mode.startswith("deterministic_greedy"):
            summary["status"] = "HEURISTIC_FEASIBLE_NOT_OPTIMAL"
        if not missing_mgt.empty and summary["status"] != "BLOCKED_POST_SOLVE_VALIDATION_FAILED":
            summary["status_before_unresolved_mgt"] = summary["status"]
            summary["status"] = "PARTIAL_RECOVERY_UNRESOLVED_MGT"
        summary.update({
            "solver_mode": solver_mode,
            "active_flights": int(len(all_active)),
            "optimization_eligible_flights": int(len(active)),
            "unresolved_mgt_flights": int(len(missing_mgt)),
            "missing_mgt_count": int(len(missing_mgt)),
            "operated_coverage_pct_of_all_flights": round(100.0 * int((decisions["recovery_action"] == "OPERATE").sum()) / max(1, len(all_active)), 2),
            "operated_coverage_pct_of_eligible_flights": round(100.0 * int((decisions["recovery_action"] == "OPERATE").sum()) / max(1, len(active)), 2),
            "existing_valid_pairing_candidates": int(existing_valid_count),
            "flights_without_existing_pairing_candidate": int(uncovered_before_pricing),
            "flights_with_one_existing_pairing_candidate": int(single_option_before_pricing),
            "three_duty_focus_flights": int(len(uncovered_flights)),
            "generated_pairing_candidates": int(generated_count),
            "candidate_scoring": (
                "not run; existing validated pairing columns only" if args.skip_pricing
                else "salary-cost-per-sector heuristic; all generated legal columns retained"
            ),
            "pricing_explored_states": int(pricing_states_explored),
            "generated_legal_pairing_domain_size": int(generated_domain_size),
            "pricing_skipped": bool(args.skip_pricing),
            "pricing_domain_status": (
                "existing_validated_pairing_report_columns_only" if args.skip_pricing
                else "all-legal-one/two-duty-domain-plus-three-duty-options-targeted-to-uncovered-flights"
            ),
            "duplicate_generated_pairings_skipped": int(duplicate_generated),
            "validated_pairing_candidates": int(len(candidate_columns)),
            "invalid_pairing_candidates_in_status_report": int((statuses["Status"] == "INVALID").sum()),
            "pairings_skipped_across_window_boundary": int(skipped),
            "selected_pairings": int(len(selected_pairings)),
            "operated_flights": int((decisions["recovery_action"] == "OPERATE").sum()),
            "cancelled_flights": int(len(cancelled)),
            "aircraft_resources": int(len(aircraft)),
            "registered_tail_inventory_loaded": bool(summary["registered_tail_inventory_loaded"]),
            "operational_acceptance": False,
            "operational_acceptance_reason": "Prototype-only: retiming/copy selection, approved tail-state reconciliation, full cost terms, and owner sign-off are not all integrated.",
            "aircraft_violations": aircraft_violations,
            "crew_coverage_errors": crew_coverage_errors,
            "solver_message": result.solver_message,
            "secondary_objective": result.secondary_objective,
            "crew_salary_cost_basis": "man_day_cost per duty calendar day; crossing midnight counts both calendar dates",
            "minimum_cancellation_count_proven": result.minimum_cancellation_count_proven,
            "full_lexicographic_optimum_proven": result.optimal_over_supplied_columns,
            "assumptions": [
                "When aircraft_inventory.csv is absent, LINE_n identifiers and starting states are inferred from AIRFLITE lines of work, not verified tail registrations.",
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
        with pd.ExcelWriter(out / f"recovery_{args.days}d.xlsx", engine="openpyxl") as writer:
            decisions.to_excel(writer, sheet_name="Flight Decisions", index=False)
            pairing_output.to_excel(writer, sheet_name="Selected Pairings", index=False)
            rotations.to_excel(writer, sheet_name="Aircraft Rotations", index=False)
            statuses.to_excel(writer, sheet_name="Pairing Validation", index=False)
            pd.DataFrame(aircraft_violations).to_excel(writer, sheet_name="Aircraft Validation", index=False)
            pd.DataFrame([summary]).to_excel(writer, sheet_name="Summary", index=False)
    except Exception as exc:
        summary["blockers"].append({"code": "RUNNER_EXCEPTION", "detail": f"{type(exc).__name__}: {exc}"})
        summary["status"] = "BLOCKED_RUNNER_EXCEPTION"
        summary["operational_acceptance"] = False
        (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps(summary, indent=2))
        return 1
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
