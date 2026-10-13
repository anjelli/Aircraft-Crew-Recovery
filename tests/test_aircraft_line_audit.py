from __future__ import annotations

import pandas as pd

from crew_recovery.aircraft_line_audit import audit_aircraft_line_transitions


def _lines():
    return pd.DataFrame([
        {
            "flight_id": "F1", "line_num": 1, "route_STD": "2026-10-25 08:00",
            "route_STA": "2026-10-25 09:00", "From": "SIN", "To": "KUL", "Eqp": "320",
        },
        {
            "flight_id": "F2", "line_num": 1, "route_STD": "2026-10-25 10:00",
            "route_STA": "2026-10-25 11:00", "From": "KUL", "To": "SIN", "Eqp": "320",
        },
        {
            "flight_id": "F3", "line_num": 1, "route_STD": "2026-10-25 12:00",
            "route_STA": "2026-10-25 13:00", "From": "SIN", "To": "BKK", "Eqp": "320",
        },
    ])


def _schedule(second_mgt=45, second_status="RESOLVED"):
    return pd.DataFrame([
        {"flight_id": "F1", "mgt_min": 45, "mgt_status": "RESOLVED"},
        {"flight_id": "F2", "mgt_min": second_mgt, "mgt_status": second_status},
        {"flight_id": "F3", "mgt_min": 45, "mgt_status": "RESOLVED"},
    ])


def test_aircraft_line_audit_accepts_station_continuous_mgt_feasible_routes():
    transitions, summary = audit_aircraft_line_transitions(_lines(), _schedule())
    assert len(transitions) == 2
    assert summary["line_count"] == 1
    assert summary["station_discontinuities"] == 0
    assert summary["time_overlaps"] == 0
    assert summary["known_mgt_shortfalls"] == 0
    assert summary["transitions_with_unresolved_predecessor_mgt"] == 0
    assert summary["full_baseline_feasibility_proven"] is True


def test_aircraft_line_audit_flags_known_mgt_shortfall():
    transitions, summary = audit_aircraft_line_transitions(_lines(), _schedule(second_mgt=75))
    assert summary["known_mgt_shortfalls"] == 1
    assert summary["full_baseline_feasibility_proven"] is False
    assert "MGT_SHORTFALL" in transitions.loc[1, "violation_codes"]


def test_aircraft_line_audit_does_not_claim_feasibility_with_unresolved_mgt():
    transitions, summary = audit_aircraft_line_transitions(
        _lines(), _schedule(second_mgt=float("nan"), second_status="MISSING_MGT_RULE")
    )
    assert summary["transitions_with_unresolved_predecessor_mgt"] == 1
    assert summary["status"] == "UNRESOLVED_MGT_RULES"
    assert summary["unresolved_mgt_flight_count"] == 1
    assert summary["full_baseline_feasibility_proven"] is False
    assert "MGT_UNRESOLVED" in transitions.loc[1, "violation_codes"]



def test_aircraft_line_audit_counts_unresolved_mgt_on_terminal_flight():
    schedule = _schedule()
    schedule.loc[schedule["flight_id"] == "F3", "mgt_min"] = float("nan")
    schedule.loc[schedule["flight_id"] == "F3", "mgt_status"] = "MISSING_MGT_RULE"
    transitions, summary = audit_aircraft_line_transitions(_lines(), schedule)
    assert summary["transitions_with_unresolved_predecessor_mgt"] == 0
    assert summary["unresolved_mgt_flight_count"] == 1
    assert summary["unresolved_mgt_flight_ids"] == ["F3"]
    assert summary["status"] == "UNRESOLVED_MGT_RULES"
    assert summary["full_baseline_feasibility_proven"] is False
