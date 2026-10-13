from datetime import datetime

import pytest

from crew_recovery.flight_copy_network import build_copy_network, feasible_copy_connection, generate_flight_copies


def _flight(fid, origin, destination, std, sta, mgt=45):
    return {
        "flight_id": fid, "From": origin, "To": destination,
        "STD": datetime.fromisoformat(std), "STA": datetime.fromisoformat(sta),
        "Eqp": "320", "mgt_min": mgt,
    }


def test_flight_copies_preserve_duration_and_create_offset_options():
    flight = _flight("F1", "SIN", "KUL", "2026-10-25T08:00:00", "2026-10-25T09:00:00")
    copies = generate_flight_copies([flight], allowed_offsets_minutes=(0, 5, 10))
    assert len(copies) == 3
    assert [c.offset_minutes for c in copies] == [0, 5, 10]
    assert all((c.sta - c.std).total_seconds() == 3600 for c in copies)


def test_copy_network_enforces_station_mgt_and_equipment():
    flights = [
        _flight("OUT", "SIN", "KUL", "2026-10-25T08:00:00", "2026-10-25T09:00:00", 45),
        _flight("BACK", "KUL", "SIN", "2026-10-25T09:45:00", "2026-10-25T10:45:00", 45),
    ]
    copies = generate_flight_copies(flights, allowed_offsets_minutes=(0, 5))
    lookup = {c.copy_id: c for c in copies}
    assert feasible_copy_connection(lookup["OUT__D00"], lookup["BACK__D00"])
    assert ("OUT__D00", "BACK__D00") in build_copy_network(copies)


def test_flight_copy_generation_fails_closed_without_mgt():
    flight = _flight("F1", "SIN", "KUL", "2026-10-25T08:00:00", "2026-10-25T09:00:00")
    flight["mgt_min"] = None
    with pytest.raises(ValueError, match="Missing resolved MGT"):
        generate_flight_copies([flight])
