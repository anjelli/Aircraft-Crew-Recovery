from datetime import datetime

import pytest

from crew_recovery.crew_costing import calendar_salary_days, crew_salary_cost


def _flight(fid, std_local, sta_local):
    return {
        "flight_id": fid,
        "STD_local": datetime.fromisoformat(std_local),
        "STA_local": datetime.fromisoformat(sta_local),
    }


def test_duty_crossing_midnight_is_charged_for_two_calendar_days():
    flights = [
        _flight("F1", "2026-10-25T22:30:00", "2026-10-25T23:30:00"),
        _flight("F2", "2026-10-26T00:30:00", "2026-10-26T01:30:00"),
    ]
    assert calendar_salary_days(flights, [[0, 1]]) == 2
    assert crew_salary_cost(flights, [[0, 1]], man_day_cost=785) == 1570


def test_two_separate_duties_charge_calendar_days_per_duty():
    flights = [
        _flight("F1", "2026-10-25T09:00:00", "2026-10-25T10:00:00"),
        _flight("F2", "2026-10-26T09:00:00", "2026-10-26T10:00:00"),
    ]
    assert calendar_salary_days(flights, [[0], [1]]) == 2
    assert crew_salary_cost(flights, [[0], [1]], man_day_cost=785) == 1570


def test_duty_splits_must_partition_all_flights():
    flights = [_flight("F1", "2026-10-25T09:00:00", "2026-10-25T10:00:00")]
    with pytest.raises(ValueError, match="partition"):
        calendar_salary_days(flights, [[]])
