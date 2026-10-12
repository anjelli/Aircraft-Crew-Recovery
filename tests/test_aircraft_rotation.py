from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from crew_recovery.aircraft_rotation import Aircraft, solve_aircraft_rotations


def _flight(fid, frm, to, departure, duration_minutes=60, equipment="320", mgt_minutes=30):
    std = datetime.fromisoformat(departure)
    return {
        "flight_id": fid,
        "From": frm,
        "To": to,
        "STD": std,
        "STA": std + timedelta(minutes=duration_minutes),
        "Eqp": equipment,
        "mgt_min": mgt_minutes,
    }


def test_aircraft_rotation_carries_station_state_across_three_flights():
    flights = [
        _flight("F1", "SIN", "KUL", "2026-10-25T08:00"),
        _flight("F2", "KUL", "BKK", "2026-10-25T09:45"),
        _flight("F3", "BKK", "SIN", "2026-10-25T11:30"),
    ]
    aircraft = [Aircraft("TEST-TAIL-1", "320", "SIN", datetime.fromisoformat("2026-10-25T07:00"))]
    result = solve_aircraft_rotations(flights, aircraft)
    assert result.assignments == {"F1": "TEST-TAIL-1", "F2": "TEST-TAIL-1", "F3": "TEST-TAIL-1"}
    assert result.rotations["TEST-TAIL-1"] == ("F1", "F2", "F3")


def test_aircraft_rotation_rejects_station_or_turnaround_discontinuity():
    flights = [
        _flight("F1", "SIN", "KUL", "2026-10-25T08:00"),
        _flight("F2", "BKK", "SIN", "2026-10-25T09:45"),
    ]
    aircraft = [Aircraft("TEST-TAIL-1", "320", "SIN", datetime.fromisoformat("2026-10-25T07:00"))]
    with pytest.raises(ValueError, match="No aircraft can serve flight 'F2'"):
        solve_aircraft_rotations(flights, aircraft)


def test_aircraft_rotation_requires_explicit_equipment_compatibility():
    flights = [_flight("F1", "SIN", "KUL", "2026-10-25T08:00", equipment="32N")]
    aircraft = [Aircraft("TEST-TAIL-1", "320", "SIN", datetime.fromisoformat("2026-10-25T07:00"))]
    with pytest.raises(ValueError, match="No aircraft can serve flight 'F1'"):
        solve_aircraft_rotations(flights, aircraft)
    result = solve_aircraft_rotations(
        flights, aircraft, compatible_equipment={"320": ["320", "32N"]}
    )
    assert result.assignments["F1"] == "TEST-TAIL-1"
