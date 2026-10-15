from __future__ import annotations

import pandas as pd
import pytest

from crew_recovery.schedule_reconciliation import build_schedule_reconciliation


def _decisions() -> pd.DataFrame:
    return pd.DataFrame([
        {
            "flight_id": "F1",
            "service_date": "2026-10-25",
            "recovery_action": "OPERATE",
            "cancellation_reason_code": None,
            "original_STD_local": "2026-10-25 08:00",
            "original_STA_local": "2026-10-25 09:00",
            "scenario_STD_local": "2026-10-25 08:15",
            "scenario_STA_local": "2026-10-25 09:15",
            "recovered_STD_local": "2026-10-25 08:15",
            "recovered_STA_local": "2026-10-25 09:15",
            "baseline_Eqp": "320",
            "Eqp": "320",
            "assigned_pairing_id": "PAIR_1",
            "assigned_aircraft_tail": "LINE_1",
        },
        {
            "flight_id": "F2",
            "service_date": "2026-10-25",
            "recovery_action": "OPERATE",
            "cancellation_reason_code": None,
            "original_STD_local": "2026-10-25 10:00",
            "original_STA_local": "2026-10-25 11:00",
            "scenario_STD_local": "2026-10-25 10:00",
            "scenario_STA_local": "2026-10-25 11:00",
            "recovered_STD_local": "2026-10-25 10:00",
            "recovered_STA_local": "2026-10-25 11:00",
            "baseline_Eqp": "32N",
            "Eqp": "320",
            "assigned_pairing_id": "PAIR_2",
            "assigned_aircraft_tail": "LINE_2",
        },
        {
            "flight_id": "F3",
            "service_date": "2026-10-25",
            "recovery_action": "CANCEL",
            "cancellation_reason_code": "NOT_SELECTED_BY_JOINT_MILP",
            "original_STD_local": "2026-10-25 12:00",
            "original_STA_local": "2026-10-25 13:00",
            "scenario_STD_local": "2026-10-25 12:00",
            "scenario_STA_local": "2026-10-25 13:00",
            "recovered_STD_local": None,
            "recovered_STA_local": None,
            "baseline_Eqp": "320",
            "Eqp": "320",
            "assigned_pairing_id": None,
            "assigned_aircraft_tail": None,
        },
        {
            "flight_id": "F4",
            "service_date": "2026-10-25",
            "recovery_action": "UNRESOLVED_MGT",
            "cancellation_reason_code": "MGT_RULE_UNRESOLVED",
            "original_STD_local": "2026-10-25 14:00",
            "original_STA_local": "2026-10-25 15:00",
            "scenario_STD_local": "2026-10-25 14:00",
            "scenario_STA_local": "2026-10-25 15:00",
            "recovered_STD_local": None,
            "recovered_STA_local": None,
            "baseline_Eqp": "320",
            "Eqp": "320",
            "assigned_pairing_id": None,
            "assigned_aircraft_tail": None,
        },
    ])


def test_reconciliation_separates_scenario_changes_from_recovery_decisions():
    report, summary = build_schedule_reconciliation(_decisions())

    assert summary["status"] == "PASS"
    assert summary["flights_compared"] == 4
    assert summary["scenario_modified_flights"] == 2
    assert summary["scenario_time_changed_flights"] == 1
    assert summary["scenario_equipment_changed_flights"] == 1
    assert summary["operated_flights_matching_scenario_times"] == 2
    assert summary["operated_flights_retimed_after_scenario"] == 0
    assert summary["cancellations_on_unchanged_scenario_flights"] == 1
    assert summary["unresolved_mgt_flights"] == 1

    by_id = report.set_index("flight_id")
    assert by_id.loc["F1", "scenario_departure_shift_minutes"] == 15
    assert bool(by_id.loc["F1", "scenario_changed_from_baseline"])
    assert bool(by_id.loc["F2", "scenario_equipment_changed"])
    assert by_id.loc["F3", "consistency_status"] == "PASS"
    assert by_id.loc["F4", "consistency_status"] == "PASS"


def test_reconciliation_fails_when_operated_times_conflict_with_scenario():
    decisions = _decisions()
    decisions.loc[decisions["flight_id"].eq("F1"), "recovered_STD_local"] = "2026-10-25 08:20"

    report, summary = build_schedule_reconciliation(decisions)
    row = report.set_index("flight_id").loc["F1"]

    assert summary["status"] == "FAIL"
    assert summary["operated_flights_retimed_after_scenario"] == 1
    assert row["consistency_status"] == "FAIL"
    assert "OPERATED_TIMES_DIFFER_FROM_SCENARIO" in row["consistency_issues"]


def test_reconciliation_rejects_duplicate_flight_ids_and_missing_columns():
    decisions = _decisions()
    with pytest.raises(ValueError, match="unique flight_id"):
        build_schedule_reconciliation(pd.concat([decisions, decisions.iloc[[0]]], ignore_index=True))
    with pytest.raises(ValueError, match="missing reconciliation columns"):
        build_schedule_reconciliation(decisions.drop(columns=["baseline_Eqp"]))


def test_reconciliation_accepts_and_reports_explicit_retiming():
    decisions = _decisions()
    decisions["retime_minutes"] = 0
    decisions.loc[decisions["flight_id"].eq("F1"), "retime_minutes"] = 15
    decisions.loc[decisions["flight_id"].eq("F1"), "recovered_STD_local"] = "2026-10-25 08:30"
    decisions.loc[decisions["flight_id"].eq("F1"), "recovered_STA_local"] = "2026-10-25 09:30"
    decisions["scenario_Eqp"] = decisions["Eqp"]
    decisions["recovered_Eqp"] = decisions["Eqp"]
    report, summary = build_schedule_reconciliation(decisions)

    assert summary["status"] == "PASS"
    assert summary["operated_flights_matching_scenario_times"] == 1
    assert summary["operated_flights_matching_recorded_retiming"] == 2
    assert summary["operated_flights_retimed_after_scenario"] == 1
    assert summary["total_absolute_retiming_minutes"] == 15.0
    assert report.set_index("flight_id").loc["F1", "consistency_status"] == "PASS"
