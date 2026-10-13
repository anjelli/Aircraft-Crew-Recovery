"""Fail-closed loader for provisional aircraft resources from AIRFLITE lines of work."""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

import pandas as pd

from crew_recovery.aircraft_rotation import Aircraft

DEFAULT_START = "2026-10-25"
DEFAULT_END = "2027-03-27"
REQUIRED = {
    "Line Num", "Equip", "Flt Num", "Dept Sta", "Dept Date", "Dept Time",
    "Arvl Sta", "Arvl Date", "Arvl Time",
}


def _dt(day: object, hhmm: object) -> pd.Timestamp:
    date = pd.to_datetime(day, format="%d%b%y", errors="coerce")
    if pd.isna(date):
        raise ValueError(f"Invalid AIRFLITE date: {day!r}")
    try:
        value = int(float(hhmm))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Invalid AIRFLITE HHMM time: {hhmm!r}") from exc
    hour, minute = value // 100, value % 100
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Invalid AIRFLITE HHMM time: {hhmm!r}")
    return pd.Timestamp(date).normalize() + pd.Timedelta(hours=hour, minutes=minute)


def _fid(number: object, std: pd.Timestamp) -> str:
    return f"{int(float(number))}_{std.strftime('%Y%m%dT%H%M%S')}"


def load_aircraft_line_inventory(
    lines_path: str | Path,
    schedule_path: str | Path,
    *,
    start_date: str = DEFAULT_START,
    end_date: str = DEFAULT_END,
    line_numbers: Sequence[int] | None = None,
    equipment_codes: Sequence[str] | None = None,
) -> tuple[list[Aircraft], pd.DataFrame]:
    """Load all selected AIRFLITE lines and reconcile them against the canonical schedule.

    The first AIRFLITE row is metadata; the second row is the CSV header.
    LINE_n IDs and first-departure start states are provisional planning
    assumptions, not verified tail registrations, maintenance, or availability.
    By default, every line/equipment code present in the requested departure-date
    horizon is included. No cross-equipment compatibility is inferred.
    """
    start, end = pd.Timestamp(start_date).normalize(), pd.Timestamp(end_date).normalize()
    if end < start:
        raise ValueError("end_date must not precede start_date")
    selected_lines = None if line_numbers is None else {int(x) for x in line_numbers}
    if selected_lines is not None and not selected_lines:
        raise ValueError("line_numbers cannot be empty when supplied")
    selected_equipment = None if equipment_codes is None else {
        str(x).strip() for x in equipment_codes
    }
    if selected_equipment is not None and not selected_equipment:
        raise ValueError("equipment_codes cannot be empty when supplied")

    lines = pd.read_csv(lines_path, encoding="utf-8-sig", skiprows=1, low_memory=False)
    missing = sorted(REQUIRED - set(lines.columns))
    if missing:
        raise ValueError(f"AIRFLITE lines-of-work file is missing columns: {missing}")
    lines = lines.copy()
    lines["Line Num"] = pd.to_numeric(lines["Line Num"], errors="coerce")
    lines["Flt Num"] = pd.to_numeric(lines["Flt Num"], errors="coerce")
    lines["Equip"] = lines["Equip"].astype(str).str.strip()
    lines = lines.loc[lines["Line Num"].notna() & lines["Flt Num"].notna()].copy()
    if selected_lines is not None:
        lines = lines.loc[lines["Line Num"].isin(selected_lines)].copy()
    if selected_equipment is not None:
        lines = lines.loc[lines["Equip"].isin(selected_equipment)].copy()
    if lines.empty:
        raise ValueError("No AIRFLITE line rows found for requested filters")
    lines["STD"] = [_dt(d, t) for d, t in zip(lines["Dept Date"], lines["Dept Time"])]
    lines["STA"] = [_dt(d, t) for d, t in zip(lines["Arvl Date"], lines["Arvl Time"])]
    lines = lines.loc[lines["STD"].dt.normalize().between(start, end)].copy()
    if lines.empty:
        raise ValueError("No AIRFLITE line flights in requested departure-date horizon")
    lines["Line Num"] = lines["Line Num"].astype(int)
    lines["Flt Num"] = lines["Flt Num"].astype(int)
    lines["From"] = lines["Dept Sta"].astype(str).str.strip().str.upper()
    lines["To"] = lines["Arvl Sta"].astype(str).str.strip().str.upper()
    lines["Eqp"] = lines["Equip"].astype(str).str.strip()
    lines["flight_id"] = [_fid(n, t) for n, t in zip(lines["Flt Num"], lines["STD"])]

    schedule = pd.read_excel(schedule_path)
    required_schedule = {"Flt Num", "From", "To", "STD", "STA", "Eqp"}
    missing = sorted(required_schedule - set(schedule.columns))
    if missing:
        raise ValueError(f"Canonical schedule is missing columns: {missing}")
    schedule = schedule.copy()
    schedule["STD"] = pd.to_datetime(schedule["STD"], errors="coerce")
    schedule["STA"] = pd.to_datetime(schedule["STA"], errors="coerce")
    schedule["route_STD"] = pd.to_datetime(schedule["STD_sin"], errors="coerce") if "STD_sin" in schedule else schedule["STD"]
    schedule["route_STA"] = pd.to_datetime(schedule["STA_sin"], errors="coerce") if "STA_sin" in schedule else schedule["STA"]
    schedule["Flt Num" = pd.to_numeric(schedule["Flt Num"], errors="coerce")
    schedule["Eqp"] = schedule["Eqp"].astype(str).str.strip()
    schedule = schedule.loc[
        schedule["STD"].notna()
        & schedule["Flt Num"].notna()
        & schedule["STD"].dt.normalize().between(start, end)
    ].copy()
    if selected_equipment is not None:
        schedule = schedule.loc[schedule["Eqp"].isin(selected_equipment)].copy()
    schedule["From"] = schedule["From"].astype(str).str.strip().str.upper()
    schedule["To"] = schedule["To"].astype(str).str.strip().str.upper()
    schedule["flight_id"] = [_fid(n, t) for n, t in zip(schedule["Flt Num"], schedule["STD"])]

    keys = ["flight_id", "From", "To", "Eqp"]
    # Ignore export/index columns when removing repeated schedule instances.
    # The canonical flight key must remain unique after this targeted collapse.
    schedule = schedule.drop_duplicates(
        subset=["STD", "STA", "Eqp", "Flt Num", "From", "To"]
    ).copy()
    if lines["flight_id"].duplicated().any() or schedule["flight_id"].duplicated().any():
        raise ValueError("Duplicate flight IDs in AIRFLITE line/schedule input")
    schedule_keys = set(map(tuple, schedule[keys].itertuples(index=False, name=None)))
    line_keys = set(map(tuple, lines[keys].itertuples(index=False, name=None)))
    if schedule_keys != line_keys:
        raise ValueError(
            "AIRFLITE line/schedule reconciliation failed: "
            f"{len(schedule_keys - line_keys)} schedule flights lack a line; "
            f"{len(line_keys - schedule_keys)} line flights are not in schedule"
        )

    first = (
        lines.sort_values(["Line Num", "STD", "flight_id"])
        .groupby("Line Num", as_index=False)
        .first()
    )
    observed_lines = set(lines["Line Num"].astype(int))
    if selected_lines is not None and observed_lines != selected_lines:
        raise ValueError(
            f"Expected line numbers {sorted(selected_lines)}, found {sorted(observed_lines)}"
        )
    equipment_by_line = lines.groupby("Line Num")["Equip"].agg(lambda s: set(s.astype(str)))
    mixed = {int(k): sorted(v) for k, v in equipment_by_line.items() if len(v) != 1}
    if mixed:
        raise ValueError(f"AIRFLITE line changes equipment within the horizon: {mixed}")
    route_std_by_fid = schedule.set_index("flight_id")["route_STD"].to_dict()
    route_sta_by_fid = schedule.set_index("flight_id")["route_STA"].to_dict()
    lines["route_STD"] = lines["flight_id"].map(route_std_by_fid)
    lines["route_STA"] = lines["flight_id"].map(route_sta_by_fid)
    first = lines.sort_values(["Line Num", "STD", "flight_id"]).groupby("Line Num", as_index=False).first()
    aircraft = [
        Aircraft(
            tail_id=f"LINE_{int(r['Line Num'])}",
            equipment=str(r["Equip"]),
            start_station=str(r["From"]),
            available_at=pd.Timestamp(r["route_STD"]).to_pydatetime(),
        )
        for _, r in first.iterrows()
    ]
    result = lines[
        ["flight_id", "Line Num", "Flt Num", "STD", "STA", "route_STD", "route_STA", "From", "To", "Equip"]
    ].rename(columns={"Line Num": "line_num", "Flt Num": "flight_number", "Equip": "Eqp"})
    result["provisional_aircraft_id"] = result["line_num"].map(lambda x: f"LINE_{int(x)}")
    result["assignment_source"] = "AIRFLITE line-of-work; provisional, not actual tail"
    result = result.sort_values(["STD", "flight_id"]).reset_index(drop=True)
    return aircraft, result


def load_320_line_inventory(
    lines_path: str | Path,
    schedule_path: str | Path,
    *,
    start_date: str = DEFAULT_START,
    end_date: str = DEFAULT_END,
    line_numbers: Sequence[int] = (1, 2, 3, 4, 5),
) -> tuple[list[Aircraft], pd.DataFrame]:
    """Backward-compatible 320-only wrapper; use load_aircraft_line_inventory for full fleet."""
    return load_aircraft_line_inventory(
        lines_path,
        schedule_path,
        start_date=start_date,
        end_date=end_date,
        line_numbers=line_numbers,
        equipment_codes=("320",),
    )
