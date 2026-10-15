from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from crew_recovery.recovery_scenario import (
    RecoveryEvent,
    RecoveryScenario,
    apply_aircraft_events,
    apply_flight_events,
    apply_flight_retiming_events,
    baseline_scenario,
    load_recovery_scenario,
)


class FakeResolver:
    def __init__(self):
        self.calls = []

    def resolve(self, eqp, airport, country, on_date):
        self.calls.append((eqp, airport, country, pd.Timestamp(on_date).normalize()))
        return 65, "SCENARIO_MGT_RESOLVED"


def _write_scenario(path: Path, events: list[dict], **overrides):
    manifest = {
        "schema_version": 1,
        "scenario_id": "unit-test",
        "description": "Synthetic test scenario",
        "events": events,
    }
    manifest.update(overrides)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _schedule() -> pd.DataFrame:
    return pd.DataFrame([
        {
            "Day": pd.Timestamp("2026-10-25"),
            "STD_local": pd.Timestamp("2026-10-25 08:00"),
            "STA_local": pd.Timestamp("2026-10-25 09:00"),
            "STD": pd.Timestamp("2026-10-25 00:00"),
            "STA": pd.Timestamp("2026-10-25 01:00"),
            "Eqp": "320",
            "To": "KUL",
            "Arvl Ctry": "MY",
            "flight_id": "F1_20261025T080000",
            "mgt_min": 50.0,
            "mgt_status": "RESOLVED",
        },
        {
            "Day": pd.Timestamp("2026-10-25"),
            "STD_local": pd.Timestamp("2026-10-25 10:00"),
            "STA_local": pd.Timestamp("2026-10-25 11:00"),
            "STD": pd.Timestamp("2026-10-25 02:00"),
            "STA": pd.Timestamp("2026-10-25 03:00"),
            "Eqp": "32N",
            "To": "SIN",
            "Arvl Ctry": "SG",
            "flight_id": "F2_20261025T100000",
            "mgt_min": 45.0,
            "mgt_status": "RESOLVED",
        },
    ])


def test_none_scenario_is_explicit_baseline():
    scenario = load_recovery_scenario(None)
    assert scenario == baseline_scenario()
    assert scenario.scenario_id == "baseline_no_disruption"
    assert scenario.events == ()


def test_scenario_rejects_unknown_types_duplicate_ids_and_bad_delay(tmp_path):
    path = _write_scenario(path=tmp_path / "bad.json", events=[
        {"event_id": "e1", "type": "CANCEL_ALL", "flight_id": "F1_20261025T080000"}
    ])
    with pytest.raises(ValueError, match="type must be one of"):
        load_recovery_scenario(path)

    path = _write_scenario(path=tmp_path / "type-array.json", events=[
        {"event_id": "e1", "type": ["FLIGHT_DELAY"], "flight_id": "F1_20261025T080000", "delay_minutes": 5}
    ])
    with pytest.raises(ValueError, match="type must be one of"):
        load_recovery_scenario(path)

    path = _write_scenario(path=tmp_path / "duplicate.json", events=[
        {"event_id": "e1", "type": "FLIGHT_DELAY", "flight_id": "F1_20261025T080000", "delay_minutes": 5},
        {"event_id": "e1", "type": "FLIGHT_EQUIPMENT_CHANGE", "flight_id": "F1_20261025T080000", "equipment_code": "32N"},
    ])
    with pytest.raises(ValueError, match="event_id values must be unique"):
        load_recovery_scenario(path)

    path = _write_scenario(path=tmp_path / "bool-delay.json", events=[
        {"event_id": "e1", "type": "FLIGHT_DELAY", "flight_id": "F1_20261025T080000", "delay_minutes": True}
    ])
    with pytest.raises(ValueError, match="positive integer"):
        load_recovery_scenario(path)


def test_flight_delay_and_equipment_change_preserve_id_and_reresolve_mgt(tmp_path):
    path = _write_scenario(path=tmp_path / "scenario.json", events=[
        {"event_id": "delay-f1", "type": "FLIGHT_DELAY", "flight_id": "F1_20261025T080000", "delay_minutes": 15},
        {"event_id": "eqp-f1", "type": "FLIGHT_EQUIPMENT_CHANGE", "flight_id": "F1_20261025T080000", "equipment_code": "32N"},
    ])
    scenario = load_recovery_scenario(path)
    resolver = FakeResolver()
    updated, summary = apply_flight_events(
        _schedule(), scenario, resolver,
        start_date="2026-10-25", end_date="2026-10-31",
    )

    flight = updated.set_index("flight_id").loc["F1_20261025T080000"]
    assert flight["STD_local"] == pd.Timestamp("2026-10-25 08:15")
    assert flight["STA_local"] == pd.Timestamp("2026-10-25 09:15")
    assert flight["STD"] == pd.Timestamp("2026-10-25 00:15")
    assert flight["STA"] == pd.Timestamp("2026-10-25 01:15")
    assert flight.name == "F1_20261025T080000"
    assert flight["Day"] == pd.Timestamp("2026-10-25")
    assert flight["Eqp"] == "32N"
    assert flight["mgt_min"] == 65
    assert flight["mgt_status"] == "SCENARIO_MGT_RESOLVED"
    assert len(resolver.calls) == 1
    assert summary["flight_events_applied"] == 2
    assert summary["affected_flight_ids"] == ["F1_20261025T080000"]


def test_event_equipment_code_must_exist_in_canonical_schedule(tmp_path):
    path = _write_scenario(path=tmp_path / "scenario.json", events=[
        {"event_id": "eqp-f1", "type": "FLIGHT_EQUIPMENT_CHANGE", "flight_id": "F1_20261025T080000", "equipment_code": "UNKNOWN_TYPE"}
    ])
    with pytest.raises(ValueError, match="not present in the canonical schedule"):
        apply_flight_events(
            _schedule(), load_recovery_scenario(path), FakeResolver(),
            start_date="2026-10-25", end_date="2026-10-31",
        )


def test_events_outside_horizon_are_skipped_and_unknown_ids_fail(tmp_path):
    path = _write_scenario(path=tmp_path / "scenario.json", events=[
        {"event_id": "delay-f1", "type": "FLIGHT_DELAY", "flight_id": "F1_20261025T080000", "delay_minutes": 20}
    ])
    scenario = load_recovery_scenario(path)
    resolver = FakeResolver()
    updated, summary = apply_flight_events(
        _schedule(), scenario, resolver,
        start_date="2026-11-01", end_date="2026-11-07",
    )
    assert updated.iloc[0]["STD_local"] == pd.Timestamp("2026-10-25 08:00")
    assert summary["skipped_outside_window_event_ids"] == ["delay-f1"]
    assert resolver.calls == []

    unknown_path = _write_scenario(path=tmp_path / "unknown.json", events=[
        {"event_id": "delay-unknown", "type": "FLIGHT_DELAY", "flight_id": "NOT_A_FLIGHT", "delay_minutes": 5}
    ])
    with pytest.raises(ValueError, match="unknown flight_id"):
        apply_flight_events(
            _schedule(), load_recovery_scenario(unknown_path), resolver,
            start_date="2026-10-25", end_date="2026-10-31",
        )


def test_aircraft_unavailability_is_whole_run_and_fails_closed():
    event = RecoveryEvent(
        event_id="tail-down", event_type="AIRCRAFT_UNAVAILABLE", aircraft_id="LINE_1"
    )
    scenario = RecoveryScenario(
        schema_version=1, scenario_id="unavailable", description="", events=(event,)
    )
    aircraft = [SimpleNamespace(tail_id="LINE_1"), SimpleNamespace(tail_id="LINE_2")]
    remaining, unavailable = apply_aircraft_events(aircraft, scenario)
    assert [item.tail_id for item in remaining] == ["LINE_2"]
    assert unavailable == ["LINE_1"]

    unknown_scenario = RecoveryScenario(
        schema_version=1,
        scenario_id="unknown",
        description="",
        events=(RecoveryEvent("bad-tail", "AIRCRAFT_UNAVAILABLE", aircraft_id="TAIL_X"),),
    )
    with pytest.raises(ValueError, match="unknown aircraft_id"):
        apply_aircraft_events(aircraft, unknown_scenario)
    with pytest.raises(ValueError, match="every aircraft"):
        apply_aircraft_events(aircraft[:1], scenario)


def test_retiming_is_applied_after_disruption_and_preserves_original_schedule_floor(tmp_path):
    path = _write_scenario(path=tmp_path / "retime.json", events=[
        {"event_id": "delay-f1", "type": "FLIGHT_DELAY", "flight_id": "F1_20261025T080000", "delay_minutes": 45},
        {"event_id": "retime-f1", "type": "FLIGHT_RETIME", "flight_id": "F1_20261025T080000", "offset_minutes": 15},
    ])
    scenario = load_recovery_scenario(path)
    resolver = FakeResolver()
    original = _schedule()
    baseline_values = original.set_index("flight_id")[["STD_local", "STA_local", "Eqp"]].copy()
    disrupted, disruption_summary = apply_flight_events(
        original, scenario, resolver, start_date="2026-10-25", end_date="2026-10-31",
    )
    assert disruption_summary["flight_events_applied"] == 1
    assert disrupted.loc[0, "STD_local"] == pd.Timestamp("2026-10-25 08:45")

    recovered, retiming_summary = apply_flight_retiming_events(
        disrupted, scenario, resolver,
        start_date="2026-10-25", end_date="2026-10-31",
        baseline_values=baseline_values,
    )
    flight = recovered.set_index("flight_id").loc["F1_20261025T080000"]
    assert flight["STD_local"] == pd.Timestamp("2026-10-25 09:00")
    assert flight["STA_local"] == pd.Timestamp("2026-10-25 10:00")
    assert retiming_summary["offsets_by_flight"] == {"F1_20261025T080000": 15}
    assert retiming_summary["retimed_flight_ids"] == ["F1_20261025T080000"]


def test_retiming_rejects_offsets_outside_documented_tiers(tmp_path):
    path = _write_scenario(path=tmp_path / "unsupported-offset.json", events=[
        {"event_id": "retime-f1", "type": "FLIGHT_RETIME", "flight_id": "F1_20261025T080000", "offset_minutes": -5},
    ])
    with pytest.raises(ValueError, match="between 0 and 15"):
        load_recovery_scenario(path)

    too_large = _write_scenario(path=tmp_path / "too-large-offset.json", events=[
        {"event_id": "retime-f1", "type": "FLIGHT_RETIME", "flight_id": "F1_20261025T080000", "offset_minutes": 20},
    ])
    with pytest.raises(ValueError, match="between 0 and 15"):
        load_recovery_scenario(too_large)
