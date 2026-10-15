"""Versioned, explicit disruption scenarios for reproducible recovery runs.

This first schema deliberately supports only disruptions represented by the
current operate/cancel model: flight delays, equipment-code changes, and
whole-run aircraft unavailability. It does not imply optimized retiming,
aircraft swaps, or time-bounded maintenance modelling.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd


SCHEMA_VERSION = 1
SUPPORTED_EVENT_TYPES = {
    "FLIGHT_DELAY",
    "FLIGHT_EQUIPMENT_CHANGE",
    "FLIGHT_RETIME",
    "AIRCRAFT_UNAVAILABLE",
}
_TOP_LEVEL_KEYS = {"schema_version", "scenario_id", "description", "events"}


@dataclass(frozen=True)
class RecoveryEvent:
    event_id: str
    event_type: str
    flight_id: str | None = None
    delay_minutes: int | None = None
    offset_minutes: int | None = None
    equipment_code: str | None = None
    aircraft_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "event_id": self.event_id,
            "type": self.event_type,
        }
        if self.flight_id is not None:
            row["flight_id"] = self.flight_id
        if self.delay_minutes is not None:
            row["delay_minutes"] = self.delay_minutes
        if self.offset_minutes is not None:
            row["offset_minutes"] = self.offset_minutes
        if self.equipment_code is not None:
            row["equipment_code"] = self.equipment_code
        if self.aircraft_id is not None:
            row["aircraft_id"] = self.aircraft_id
        return row


@dataclass(frozen=True)
class RecoveryScenario:
    schema_version: int
    scenario_id: str
    description: str
    events: tuple[RecoveryEvent, ...]
    source_path: str | None = None

    def manifest_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "scenario_id": self.scenario_id,
            "description": self.description,
            "events": [event.as_dict() for event in self.events],
            "source_path": self.source_path,
        }


def baseline_scenario() -> RecoveryScenario:
    """Return the explicit no-disruption scenario used by default."""
    return RecoveryScenario(
        schema_version=SCHEMA_VERSION,
        scenario_id="baseline_no_disruption",
        description="No additional disruption events; use the prepared source schedule.",
        events=(),
        source_path=None,
    )


def _non_empty_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _parse_event(raw: object, index: int) -> RecoveryEvent:
    if not isinstance(raw, dict):
        raise ValueError(f"events[{index}] must be a JSON object")
    event_type = raw.get("type")
    if not isinstance(event_type, str) or event_type not in SUPPORTED_EVENT_TYPES:
        raise ValueError(
            f"events[{index}].type must be one of {sorted(SUPPORTED_EVENT_TYPES)}"
        )
    event_id = _non_empty_string(raw.get("event_id"), f"events[{index}].event_id")
    if event_type == "FLIGHT_DELAY":
        expected = {"event_id", "type", "flight_id", "delay_minutes"}
        if set(raw) != expected:
            raise ValueError(
                f"{event_type} event {event_id!r} requires exactly {sorted(expected)}"
            )
        flight_id = _non_empty_string(raw["flight_id"], f"event {event_id!r}.flight_id")
        delay = raw["delay_minutes"]
        if isinstance(delay, bool) or not isinstance(delay, int) or delay <= 0:
            raise ValueError(f"event {event_id!r}.delay_minutes must be a positive integer")
        return RecoveryEvent(event_id, event_type, flight_id=flight_id, delay_minutes=delay)

    if event_type == "FLIGHT_RETIME":
        expected = {"event_id", "type", "flight_id", "offset_minutes"}
        if set(raw) != expected:
            raise ValueError(
                f"{event_type} event {event_id!r} requires exactly {sorted(expected)}"
            )
        offset = raw["offset_minutes"]
        if isinstance(offset, bool) or not isinstance(offset, int) or abs(offset) > 180:
            raise ValueError(
                f"event {event_id!r}.offset_minutes must be an integer between -180 and 180"
            )
        return RecoveryEvent(
            event_id, event_type,
            flight_id=_non_empty_string(raw["flight_id"], f"event {event_id!r}.flight_id"),
            offset_minutes=offset,
        )

    if event_type == "FLIGHT_EQUIPMENT_CHANGE":
        expected = {"event_id", "type", "flight_id", "equipment_code"}
        if set(raw) != expected:
            raise ValueError(
                f"{event_type} event {event_id!r} requires exactly {sorted(expected)}"
            )
        return RecoveryEvent(
            event_id,
            event_type,
            flight_id=_non_empty_string(raw["flight_id"], f"event {event_id!r}.flight_id"),
            equipment_code=_non_empty_string(
                raw["equipment_code"], f"event {event_id!r}.equipment_code"
            ).upper(),
        )

    expected = {"event_id", "type", "aircraft_id"}
    if set(raw) != expected:
        raise ValueError(
            f"{event_type} event {event_id!r} requires exactly {sorted(expected)}"
        )
    return RecoveryEvent(
        event_id,
        event_type,
        aircraft_id=_non_empty_string(raw["aircraft_id"], f"event {event_id!r}.aircraft_id"),
    )


def load_recovery_scenario(path: str | Path | None) -> RecoveryScenario:
    """Load and validate a versioned JSON manifest; None means baseline."""
    if path is None:
        return baseline_scenario()
    manifest_path = Path(path).expanduser().resolve()
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Cannot read scenario manifest {manifest_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in scenario manifest {manifest_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError("Scenario manifest must be a JSON object")
    extra = set(raw) - _TOP_LEVEL_KEYS
    missing = {"schema_version", "scenario_id", "events"} - set(raw)
    if extra or missing:
        raise ValueError(
            f"Scenario manifest keys invalid; missing={sorted(missing)}, extra={sorted(extra)}"
        )
    version = raw["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
        raise ValueError(f"Unsupported schema_version {version!r}; expected {SCHEMA_VERSION}")
    scenario_id = _non_empty_string(raw["scenario_id"], "scenario_id")
    description = raw.get("description", "")
    if not isinstance(description, str):
        raise ValueError("description must be a string")
    events_raw = raw["events"]
    if not isinstance(events_raw, list):
        raise ValueError("events must be a JSON array")
    events = tuple(_parse_event(event, i) for i, event in enumerate(events_raw))
    event_ids = [event.event_id for event in events]
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("event_id values must be unique within a scenario")

    targets: set[tuple[str, str]] = set()
    for event in events:
        if event.event_type == "AIRCRAFT_UNAVAILABLE":
            target = ("AIRCRAFT_UNAVAILABLE", str(event.aircraft_id))
        else:
            target = (event.event_type, str(event.flight_id))
        if target in targets:
            raise ValueError(
                f"Duplicate event type/target {target}; combine or disambiguate the scenario events"
            )
        targets.add(target)

    return RecoveryScenario(
        schema_version=version,
        scenario_id=scenario_id,
        description=description.strip(),
        events=events,
        source_path=str(manifest_path),
    )


def apply_flight_events(
    prepared_schedule: pd.DataFrame,
    scenario: RecoveryScenario,
    resolver: object,
    *,
    start_date: object,
    end_date: object,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Apply scheduled flight events without changing the canonical flight_id.

    A flight enters a horizon based on its original service/departure date (Day).
    Delaying a flight across midnight does not silently move it to another horizon.
    MGT is resolved again for every flight changed by a delay or equipment event.
    """
    required = {
        "Day", "STD_local", "STA_local", "STD", "STA", "Eqp", "To",
        "Arvl Ctry", "flight_id", "mgt_min", "mgt_status",
    }
    missing = sorted(required - set(prepared_schedule.columns))
    if missing:
        raise ValueError(f"Prepared schedule missing columns required for scenarios: {missing}")
    schedule = prepared_schedule.copy()
    schedule["flight_id"] = schedule["flight_id"].astype(str)
    if schedule["flight_id"].duplicated().any():
        raise ValueError("Prepared schedule must have unique flight_id values")

    lookup = {fid: idx for idx, fid in zip(schedule.index, schedule["flight_id"])}
    active_start = pd.Timestamp(start_date).normalize()
    active_end = pd.Timestamp(end_date).normalize()
    service_days = pd.to_datetime(schedule["Day"], errors="coerce").dt.normalize()
    equipment_universe = set(schedule["Eqp"].dropna().astype(str).str.strip().str.upper())
    affected_ids: set[str] = set()
    applied_ids: list[str] = []
    skipped_outside_window: list[str] = []
    touched_events: dict[str, list[str]] = {}

    for event in scenario.events:
        if event.event_type in {"AIRCRAFT_UNAVAILABLE", "FLIGHT_RETIME"}:
            continue
        assert event.flight_id is not None
        if event.flight_id not in lookup:
            raise ValueError(
                f"Scenario event {event.event_id!r} references unknown flight_id {event.flight_id!r}"
            )
        idx = lookup[event.flight_id]
        day = service_days.loc[idx]
        if pd.isna(day):
            raise ValueError(f"Prepared schedule has invalid service date for {event.flight_id!r}")
        if day < active_start or day > active_end:
            skipped_outside_window.append(event.event_id)
            continue

        if event.event_type == "FLIGHT_DELAY":
            assert event.delay_minutes is not None
            delta = pd.Timedelta(minutes=event.delay_minutes)
            for column in ("STD_local", "STA_local", "STD", "STA"):
                schedule.loc[idx, column] = pd.Timestamp(schedule.loc[idx, column]) + delta
        elif event.event_type == "FLIGHT_EQUIPMENT_CHANGE":
            assert event.equipment_code is not None
            if event.equipment_code not in equipment_universe:
                raise ValueError(
                    f"Event {event.event_id!r} specifies equipment {event.equipment_code!r} "
                    "not present in the canonical schedule; do not invent equipment compatibility"
                )
            schedule.loc[idx, "Eqp"] = event.equipment_code
        else:
            raise ValueError(f"Unsupported flight event type {event.event_type!r}")

        affected_ids.add(event.flight_id)
        touched_events.setdefault(event.flight_id, []).append(event.event_id)
        applied_ids.append(event.event_id)

    for flight_id in sorted(affected_ids):
        idx = lookup[flight_id]
        flight = schedule.loc[idx]
        value, status = resolver.resolve(
            str(flight["Eqp"]),
            str(flight["To"]),
            str(flight["Arvl Ctry"]),
            pd.Timestamp(flight["STA_local"]).normalize(),
        )
        schedule.loc[idx, "mgt_min"] = value
        schedule.loc[idx, "mgt_status"] = status

    summary = {
        "scenario_id": scenario.scenario_id,
        "status": "APPLIED",
        "manifest_event_count": sum(
            event.event_type in {"FLIGHT_DELAY", "FLIGHT_EQUIPMENT_CHANGE"}
            for event in scenario.events
        ),
        "flight_events_applied": len(applied_ids),
        "applied_event_ids": applied_ids,
        "skipped_outside_window_event_ids": skipped_outside_window,
        "affected_flight_ids": sorted(affected_ids),
        "mgt_re_resolved_flight_ids": sorted(affected_ids),
        "event_ids_by_flight": {fid: ids for fid, ids in sorted(touched_events.items())},
        "horizon_membership": "original service/departure date (Day); delay does not change horizon membership",
    }
    return schedule, summary




def apply_flight_retiming_events(
    schedule: pd.DataFrame,
    scenario: RecoveryScenario,
    resolver: object,
    *,
    start_date: str | pd.Timestamp,
    end_date: str | pd.Timestamp,
    baseline_values: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Apply explicit retiming choices after disruption events and before revalidation.

    The retiming offset is relative to the disruption-adjusted schedule. A
    recovery copy may not depart or arrive before the original source schedule;
    this prevents a trial from silently erasing more delay than is available.
    """
    schedule = schedule.copy()
    events = [event for event in scenario.events if event.event_type == "FLIGHT_RETIME"]
    active_start = pd.Timestamp(start_date).normalize()
    active_end = pd.Timestamp(end_date).normalize()
    lookup = {str(fid): idx for idx, fid in zip(schedule.index, schedule["flight_id"])}
    if len(lookup) != len(schedule):
        raise ValueError("Prepared schedule must contain unique flight_id values")
    seen_flights: set[str] = set()
    applied: list[str] = []
    skipped: list[str] = []
    changed: list[str] = []

    for event in events:
        assert event.flight_id is not None and event.offset_minutes is not None
        fid = event.flight_id
        if fid in seen_flights:
            raise ValueError(f"Only one FLIGHT_RETIME event is allowed per flight_id: {fid!r}")
        seen_flights.add(fid)
        if fid not in lookup:
            raise ValueError(f"Retiming event {event.event_id!r} references unknown flight_id {fid!r}")
        idx = lookup[fid]
        day = pd.Timestamp(schedule.loc[idx, "Day"]).normalize()
        if day < active_start or day > active_end:
            skipped.append(event.event_id)
            continue
        if fid not in baseline_values.index:
            raise ValueError(f"Baseline schedule has no original times for flight_id {fid!r}")

        delta = pd.Timedelta(minutes=event.offset_minutes)
        new_std = pd.Timestamp(schedule.loc[idx, "STD_local"]) + delta
        new_sta = pd.Timestamp(schedule.loc[idx, "STA_local"]) + delta
        baseline_std = pd.Timestamp(baseline_values.loc[fid, "STD_local"])
        baseline_sta = pd.Timestamp(baseline_values.loc[fid, "STA_local"])
        if new_std < baseline_std or new_sta < baseline_sta:
            raise ValueError(
                f"Retiming event {event.event_id!r} would move {fid!r} before its original schedule; "
                "reduce the negative offset or increase the disruption delay"
            )
        for column in ("STD_local", "STA_local", "STD", "STA"):
            schedule.loc[idx, column] = pd.Timestamp(schedule.loc[idx, column]) + delta
        applied.append(event.event_id)
        if event.offset_minutes:
            changed.append(fid)

    for fid in changed:
        idx = lookup[fid]
        flight = schedule.loc[idx]
        value, status = resolver.resolve(
            str(flight["Eqp"]),
            str(flight["To"]),
            str(flight["Arvl Ctry"]),
            pd.Timestamp(flight["STA_local"]).normalize(),
        )
        schedule.loc[idx, "mgt_min"] = value
        schedule.loc[idx, "mgt_status"] = status

    return schedule, {
        "status": "APPLIED" if applied else "NOT_APPLIED",
        "manifest_event_count": len(events),
        "retiming_events_applied": len(applied),
        "applied_event_ids": applied,
        "skipped_outside_window_event_ids": skipped,
        "retimed_flight_ids": sorted(changed),
        "offsets_by_flight": {
            event.flight_id: int(event.offset_minutes)
            for event in events
            if event.event_id in applied
        },
        "interpretation": (
            "Retiming offsets are explicit candidate choices. The bounded recovery-search runner "
            "evaluates alternative offsets by re-solving the schedule; a single-run invocation only "
            "applies the offsets supplied in its scenario manifest."
        ),
    }

def apply_aircraft_events(
    aircraft: Sequence[object],
    scenario: RecoveryScenario,
) -> tuple[list[object], list[str]]:
    """Remove whole-run unavailable resources; time-bounded maintenance is not supported."""
    unavailable = {
        str(event.aircraft_id)
        for event in scenario.events
        if event.event_type == "AIRCRAFT_UNAVAILABLE"
    }
    resource_ids = {str(item.tail_id) for item in aircraft}
    unknown = sorted(unavailable - resource_ids)
    if unknown:
        raise ValueError(f"Scenario references unknown aircraft_id values: {unknown}")
    remaining = [item for item in aircraft if str(item.tail_id) not in unavailable]
    if aircraft and not remaining:
        raise ValueError("Scenario makes every aircraft resource unavailable")
    return remaining, sorted(unavailable)
