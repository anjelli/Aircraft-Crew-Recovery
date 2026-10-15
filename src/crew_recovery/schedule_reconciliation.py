"""Per-flight baseline, scenario, and recovery schedule reconciliation.

This report is an audit of the current operate/cancel prototype. It does not
model retiming: an operated flight must retain the scenario-adjusted times and
equipment. Unknown MGT flights remain unresolved rather than being treated as
cancellations.
"""
from __future__ import annotations

from typing import Any

import pandas as pd


_REQUIRED_COLUMNS = {
    "flight_id",
    "service_date",
    "recovery_action",
    "cancellation_reason_code",
    "original_STD_local",
    "original_STA_local",
    "scenario_STD_local",
    "scenario_STA_local",
    "recovered_STD_local",
    "recovered_STA_local",
    "baseline_Eqp",
    "Eqp",
    "assigned_pairing_id",
    "assigned_aircraft_tail",
}


def build_schedule_reconciliation(
    decisions: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Compare original, scenario-adjusted, and recovered schedule fields.

    A consistency failure means the exported decision contradicts the current
    model contract. It is not a test of full operational feasibility.
    """
    missing = sorted(_REQUIRED_COLUMNS - set(decisions.columns))
    if missing:
        raise ValueError(f"Flight decisions missing reconciliation columns: {missing}")
    if decisions["flight_id"].astype(str).duplicated().any():
        raise ValueError("Flight decisions must contain unique flight_id values")

    frame = decisions.copy().reset_index(drop=True)
    action = frame["recovery_action"].astype(str).str.strip().str.upper()

    original_std = pd.to_datetime(frame["original_STD_local"], errors="coerce")
    original_sta = pd.to_datetime(frame["original_STA_local"], errors="coerce")
    scenario_std = pd.to_datetime(frame["scenario_STD_local"], errors="coerce")
    scenario_sta = pd.to_datetime(frame["scenario_STA_local"], errors="coerce")
    recovered_std = pd.to_datetime(frame["recovered_STD_local"], errors="coerce")
    recovered_sta = pd.to_datetime(frame["recovered_STA_local"], errors="coerce")

    baseline_equipment = frame["baseline_Eqp"].fillna("").astype(str).str.strip()
    scenario_equipment = frame["Eqp"].fillna("").astype(str).str.strip()
    equipment_changed = baseline_equipment.ne(scenario_equipment)
    baseline_equipment_missing = baseline_equipment.eq("")
    scenario_equipment_missing = scenario_equipment.eq("")
    departure_shift = (scenario_std - original_std).dt.total_seconds() / 60.0
    arrival_shift = (scenario_sta - original_sta).dt.total_seconds() / 60.0
    recovered_departure_delta = (recovered_std - scenario_std).dt.total_seconds() / 60.0
    recovered_arrival_delta = (recovered_sta - scenario_sta).dt.total_seconds() / 60.0

    # Invalid/missing timestamps should be reported as data errors, not counted
    # as disruption-induced changes.
    scenario_times_changed = (
        departure_shift.notna() & departure_shift.ne(0)
    ) | (
        arrival_shift.notna() & arrival_shift.ne(0)
    )
    scenario_changed = scenario_times_changed | equipment_changed
    operated = action.eq("OPERATE")
    cancelled = action.eq("CANCEL")
    unresolved = action.eq("UNRESOLVED_MGT")

    matches_scenario = (
        recovered_std.notna()
        & recovered_sta.notna()
        & scenario_std.notna()
        & scenario_sta.notna()
        & recovered_std.eq(scenario_std)
        & recovered_sta.eq(scenario_sta)
    )
    recovered_equipment_source = (
        frame["recovered_Eqp"].fillna("").astype(str).str.strip()
        if "recovered_Eqp" in frame.columns
        else scenario_equipment
    )
    recovered_equipment = recovered_equipment_source.where(operated, other=pd.NA)
    if "retime_minutes" in frame.columns:
        retime_minutes = pd.to_numeric(frame["retime_minutes"], errors="coerce")
    else:
        retime_minutes = pd.Series(0.0, index=frame.index)
    expected_recovered_std = scenario_std + pd.to_timedelta(retime_minutes, unit="m")
    expected_recovered_sta = scenario_sta + pd.to_timedelta(retime_minutes, unit="m")
    matches_recorded_retiming = (
        retime_minutes.notna()
        & recovered_std.notna()
        & recovered_sta.notna()
        & expected_recovered_std.notna()
        & expected_recovered_sta.notna()
        & recovered_std.eq(expected_recovered_std)
        & recovered_sta.eq(expected_recovered_sta)
    )

    issues_by_row: list[str] = []
    for idx in range(len(frame)):
        issues: list[str] = []
        if pd.isna(original_std.iloc[idx]) or pd.isna(original_sta.iloc[idx]):
            issues.append("BASELINE_TIME_MISSING_OR_INVALID")
        if pd.isna(scenario_std.iloc[idx]) or pd.isna(scenario_sta.iloc[idx]):
            issues.append("SCENARIO_TIME_MISSING_OR_INVALID")
        if baseline_equipment_missing.iloc[idx]:
            issues.append("BASELINE_EQUIPMENT_MISSING")
        if scenario_equipment_missing.iloc[idx]:
            issues.append("SCENARIO_EQUIPMENT_MISSING")

        row_action = action.iloc[idx]
        recovered_time_present = pd.notna(recovered_std.iloc[idx]) or pd.notna(recovered_sta.iloc[idx])
        pairing_id = frame["assigned_pairing_id"].iloc[idx]
        aircraft_id = frame["assigned_aircraft_tail"].iloc[idx]
        has_pairing = pd.notna(pairing_id) and bool(str(pairing_id).strip())
        has_aircraft = pd.notna(aircraft_id) and bool(str(aircraft_id).strip())

        if row_action == "OPERATE":
            if not matches_recorded_retiming.iloc[idx]:
                if (
                    pd.notna(retime_minutes.iloc[idx])
                    and abs(float(retime_minutes.iloc[idx])) < 1e-9
                ):
                    issues.append("OPERATED_TIMES_DIFFER_FROM_SCENARIO")
                else:
                    issues.append("OPERATED_TIMES_DO_NOT_MATCH_RECORDED_RETIMING")
            if not has_pairing:
                issues.append("OPERATED_FLIGHT_MISSING_PAIRING")
            if not has_aircraft:
                issues.append("OPERATED_FLIGHT_MISSING_AIRCRAFT")
        elif row_action in {"CANCEL", "UNRESOLVED_MGT"}:
            if recovered_time_present:
                issues.append("NON_OPERATED_FLIGHT_HAS_RECOVERED_TIME")
            if has_pairing or has_aircraft:
                issues.append("NON_OPERATED_FLIGHT_HAS_RESOURCE_ASSIGNMENT")
            if row_action == "UNRESOLVED_MGT" and str(
                frame["cancellation_reason_code"].iloc[idx]
            ) != "MGT_RULE_UNRESOLVED":
                issues.append("UNRESOLVED_MGT_REASON_CODE_MISMATCH")
        else:
            issues.append("UNKNOWN_RECOVERY_ACTION")

        issues_by_row.append(";".join(issues))

    report = pd.DataFrame({
        "flight_id": frame["flight_id"].astype(str),
        "service_date": frame["service_date"],
        "recovery_action": action,
        "cancellation_reason_code": frame["cancellation_reason_code"],
        "baseline_STD_local": frame["original_STD_local"],
        "scenario_STD_local": frame["scenario_STD_local"],
        "recovered_STD_local": frame["recovered_STD_local"],
        "scenario_departure_shift_minutes": departure_shift,
        "baseline_STA_local": frame["original_STA_local"],
        "scenario_STA_local": frame["scenario_STA_local"],
        "recovered_STA_local": frame["recovered_STA_local"],
        "scenario_arrival_shift_minutes": arrival_shift,
        "baseline_Eqp": frame["baseline_Eqp"],
        "scenario_Eqp": frame["scenario_Eqp"] if "scenario_Eqp" in frame.columns else frame["Eqp"],
        "recovered_Eqp": recovered_equipment,
        "retime_minutes": retime_minutes,
        "scenario_departure_or_arrival_changed": scenario_times_changed,
        "scenario_equipment_changed": equipment_changed,
        "scenario_changed_from_baseline": scenario_changed,
        "recovered_departure_shift_from_scenario_minutes": recovered_departure_delta,
        "recovered_arrival_shift_from_scenario_minutes": recovered_arrival_delta,
        "recovered_times_match_scenario": matches_scenario.where(operated, other=pd.NA),
        "assigned_pairing_id": frame["assigned_pairing_id"],
        "assigned_aircraft_tail": frame["assigned_aircraft_tail"],
        "consistency_status": ["FAIL" if item else "PASS" for item in issues_by_row],
        "consistency_issues": issues_by_row,
    })

    failures = int(report["consistency_status"].eq("FAIL").sum())
    operated_count = int(operated.sum())
    summary: dict[str, Any] = {
        "status": "PASS" if failures == 0 else "FAIL",
        "flights_compared": int(len(report)),
        "scenario_modified_flights": int(scenario_changed.sum()),
        "scenario_time_changed_flights": int(scenario_times_changed.sum()),
        "scenario_equipment_changed_flights": int(equipment_changed.sum()),
        "operated_flights": operated_count,
        "operated_flights_matching_scenario_times": int((operated & matches_scenario).sum()),
        "operated_flights_matching_recorded_retiming": int((operated & matches_recorded_retiming).sum()),
        "operated_flights_retimed_after_scenario": int((operated & ~matches_scenario).sum()),
        "total_absolute_retiming_minutes": float(retime_minutes.loc[operated].abs().sum()),
        "cancelled_flights": int(cancelled.sum()),
        "cancellations_on_unchanged_scenario_flights": int((cancelled & ~scenario_changed).sum()),
        "cancellations_on_scenario_modified_flights": int((cancelled & scenario_changed).sum()),
        "unresolved_mgt_flights": int(unresolved.sum()),
        "consistency_failures": failures,
        "interpretation": (
            "This audit checks exported schedule reconciliation and assignment presence only; "
            "it is not a full operational-feasibility certificate. The current model does not "
            "optimize retiming in the native MILP, so operated-flight times are checked against the explicit selected retiming offsets."
        ),
    }
    return report, summary
