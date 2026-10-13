"""Independent transition audit for supplied AIRFLITE aircraft lines."""
from __future__ import annotations

from typing import Any

import pandas as pd


LINE_COLUMNS = {
    "flight_id", "line_num", "route_STD", "route_STA", "From", "To", "Eqp"
}
SCHEDULE_COLUMNS = {
    "flight_id", "STD", "STA", "From", "To", "Eqp", "mgt_min", "mgt_status"
}


def audit_aircraft_line_transitions(
    line_assignments: pd.DataFrame,
    prepared_schedule: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Audit every consecutive transition in the supplied aircraft line plan.

    Uses common-time route_STD/route_STA for aircraft movement and the resolved
    MGT attached to the preceding flight. A line plan with unresolved MGT is not
    declared feasible even when no other transition violation is observed.
    """
    missing_lines = sorted(LINE_COLUMNS - set(line_assignments.columns))
    missing_schedule = sorted(SCHEDULE_COLUMNS - set(prepared_schedule.columns))
    if missing_lines:
        raise ValueError(f"Aircraft line assignments missing columns: {missing_lines}")
    if missing_schedule:
        raise ValueError(f"Prepared schedule missing columns: {missing_schedule}")

    lines = line_assignments.copy()
    schedule = prepared_schedule[
        ["flight_id", "mgt_min", "mgt_status"]
    ].copy()
    lines["flight_id"] = lines["flight_id"].astype(str)
    schedule["flight_id"] = schedule["flight_id"].astype(str)
    if schedule["flight_id"].duplicated().any():
        raise ValueError("Prepared schedule flight_id must be unique")
    if lines["flight_id"].duplicated().any():
        raise ValueError("Aircraft line assignment flight_id must be unique")

    lines["route_STD"] = pd.to_datetime(lines["route_STD"], errors="coerce")
    lines["route_STA"] = pd.to_datetime(lines["route_STA"], errors="coerce")
    if lines[["route_STD", "route_STA"]].isna().any(axis=None):
        raise ValueError("Aircraft line assignments contain invalid common-time route timestamps")
    merged = lines.merge(schedule, on="flight_id", how="left", validate="one_to_one", indicator=True)
    missing_mgt_rows = merged.loc[merged["_merge"] != "both", "flight_id"].tolist()
    if missing_mgt_rows:
        raise ValueError(
            "Line assignments could not be reconciled to prepared schedule for flight IDs: "
            + ", ".join(missing_mgt_rows[:10])
        )
    merged = merged.drop(columns=["_merge"])
    merged["mgt_min"] = pd.to_numeric(merged["mgt_min"], errors="coerce")
    merged["mgt_status"] = merged["mgt_status"].astype(str)
    merged = merged.sort_values(["line_num", "route_STD", "flight_id"]).reset_index(drop=True)

    transitions: list[dict[str, Any]] = []
    for line_num, group in merged.groupby("line_num", sort=True):
        rows = list(group.to_dict("records"))
        for previous, current in zip(rows, rows[1:]):
            gap = (current["route_STD"] - previous["route_STA"]).total_seconds() / 60.0
            raw_mgt = previous.get("mgt_min")
            mgt_status = str(previous.get("mgt_status", ""))
            mgt_resolved = pd.notna(raw_mgt) and not any(
                token in mgt_status.upper()
                for token in ("_CONFLICT", "MISSING", "OUT_OF_EFFECTIVE_DATE", "UNRESOLVED")
            )
            required_mgt = float(raw_mgt) if mgt_resolved else None
            previous_to = str(previous["To"]).strip().upper()
            current_from = str(current["From"]).strip().upper()
            codes: list[str] = []
            if previous_to != current_from:
                codes.append("STATION_DISCONTINUITY")
            if gap < -1e-9:
                codes.append("TIME_OVERLAP")
            if not mgt_resolved:
                codes.append("MGT_UNRESOLVED")
            elif gap + 1e-9 < float(required_mgt):
                codes.append("MGT_SHORTFALL")
            transitions.append({
                "line_num": int(line_num),
                "previous_flight_id": str(previous["flight_id"]),
                "next_flight_id": str(current["flight_id"]),
                "previous_to": previous_to,
                "next_from": current_from,
                "previous_route_STA": previous["route_STA"].isoformat(),
                "next_route_STD": current["route_STD"].isoformat(),
                "ground_gap_minutes": round(float(gap), 3),
                "required_mgt_minutes": required_mgt,
                "previous_mgt_status": mgt_status,
                "transition_status": "FEASIBLE_ON_CHECKED_RULES" if not codes else "REVIEW_REQUIRED",
                "violation_codes": ";".join(codes),
            })

    transition_df = pd.DataFrame(transitions, columns=[
        "line_num", "previous_flight_id", "next_flight_id", "previous_to",
        "next_from", "previous_route_STA", "next_route_STD",
        "ground_gap_minutes", "required_mgt_minutes", "previous_mgt_status",
        "transition_status", "violation_codes",
    ])
    if transition_df.empty:
        station_discontinuities = time_overlaps = mgt_shortfalls = unresolved = 0
    else:
        codes = transition_df["violation_codes"].fillna("")
        station_discontinuities = int(codes.str.contains("STATION_DISCONTINUITY", regex=False).sum())
        time_overlaps = int(codes.str.contains("TIME_OVERLAP", regex=False).sum())
        mgt_shortfalls = int(codes.str.contains("MGT_SHORTFALL", regex=False).sum())
        unresolved = int(codes.str.contains("MGT_UNRESOLVED", regex=False).sum())

    hard_violations = station_discontinuities + time_overlaps + mgt_shortfalls
    if hard_violations:
        status = "INFEASIBLE_TRANSITIONS_FOUND"
    elif unresolved:
        status = "UNRESOLVED_MGT_RULES"
    else:
        status = "FEASIBLE_ON_CHECKED_RULES"

    summary = {
        "status": status,
        "line_count": int(merged["line_num"].nunique()),
        "line_assignment_flights": int(len(merged)),
        "unique_line_assignment_flights": int(merged["flight_id"].nunique()),
        "consecutive_transitions_checked": int(len(transition_df)),
        "station_discontinuities": station_discontinuities,
        "time_overlaps": time_overlaps,
        "known_mgt_shortfalls": mgt_shortfalls,
        "transitions_with_unresolved_predecessor_mgt": unresolved,
        "transitions_feasible_on_checked_rules": int(
            (transition_df["transition_status"] == "FEASIBLE_ON_CHECKED_RULES").sum()
        ) if not transition_df.empty else 0,
        "full_baseline_feasibility_proven": status == "FEASIBLE_ON_CHECKED_RULES",
        "interpretation": (
            "The supplied line plan passes the checked station, time, and resolved-MGT tests."
            if status == "FEASIBLE_ON_CHECKED_RULES" else
            "At least one known aircraft transition violation exists; inspect the transition CSV."
            if status == "INFEASIBLE_TRANSITIONS_FOUND" else
            "No known transition violation was detected, but unresolved MGT prevents declaring the entire baseline feasible."
        ),
    }
    return transition_df, summary
