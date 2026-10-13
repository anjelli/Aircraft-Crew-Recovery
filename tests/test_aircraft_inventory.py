import pandas as pd
import pytest

from crew_recovery.aircraft_inventory import load_registered_aircraft_inventory


def test_loads_verified_registered_tails(tmp_path):
    path = tmp_path / "aircraft_inventory.csv"
    pd.DataFrame([{
        "tail_id": "9V-ABC",
        "equipment": "320",
        "start_station": "SIN",
        "available_at": "2026-10-25T08:00:00",
        "verified": "true",
        "source": "fleet roster export",
    }]).to_csv(path, index=False)

    aircraft = load_registered_aircraft_inventory(path)
    assert len(aircraft) == 1
    assert aircraft[0].tail_id == "9V-ABC"
    assert aircraft[0].equipment == "320"
    assert aircraft[0].start_station == "SIN"


def test_rejects_provisional_line_id_as_tail_registration(tmp_path):
    path = tmp_path / "aircraft_inventory.csv"
    pd.DataFrame([{
        "tail_id": "LINE_1",
        "equipment": "320",
        "start_station": "SIN",
        "available_at": "2026-10-25T08:00:00",
        "verified": "true",
        "source": "AIRFLITE lines of work",
    }]).to_csv(path, index=False)

    with pytest.raises(ValueError, match="provisional line identifier"):
        load_registered_aircraft_inventory(path)


def test_rejects_unverified_tail_rows(tmp_path):
    path = tmp_path / "aircraft_inventory.csv"
    pd.DataFrame([{
        "tail_id": "9V-ABC",
        "equipment": "320",
        "start_station": "SIN",
        "available_at": "2026-10-25T08:00:00",
        "verified": "false",
        "source": "inference",
    }]).to_csv(path, index=False)

    with pytest.raises(ValueError, match="verified=true"):
        load_registered_aircraft_inventory(path)
