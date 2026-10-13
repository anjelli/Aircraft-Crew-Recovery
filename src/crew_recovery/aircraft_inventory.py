"""Loader for verified aircraft tail registrations and initial states."""
from __future__ import annotations

from pathlib import Path
import pandas as pd

from crew_recovery.aircraft_rotation import Aircraft

REQUIRED = {"tail_id", "equipment", "start_station", "available_at", "verified", "source"}


def load_registered_aircraft_inventory(path: str | Path) -> list[Aircraft]:
    """Load only explicitly verified registrations; reject provisional line labels."""
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    missing = sorted(REQUIRED - set(frame.columns))
    if missing:
        raise ValueError(f"Registered aircraft inventory missing columns: {missing}")
    if frame.empty:
        raise ValueError("Registered aircraft inventory contains no aircraft")
    frame = frame.copy()
    for col in ("tail_id", "equipment", "start_station", "available_at", "source"):
        frame[col] = frame[col].astype(str).str.strip()
    if (frame[list(REQUIRED)] == "").any(axis=None):
        raise ValueError("Registered aircraft inventory contains blank required values")
    frame["verified"] = frame["verified"].astype(str).str.strip().str.lower()
    if not frame["verified"].isin({"true", "yes", "1"}).all():
        raise ValueError("Every aircraft must have verified=true; provisional LINE_n IDs are not tail registrations")
    if frame["tail_id"].duplicated().any():
        duplicates = sorted(frame.loc[frame["tail_id"].duplicated(keep=False), "tail_id"].unique())
        raise ValueError(f"Duplicate aircraft tail registrations: {duplicates}")
    if frame["tail_id"].str.upper().str.startswith("LINE_").any():
        raise ValueError("LINE_n is a provisional line identifier, not a verified tail registration")
    frame["available_at"] = pd.to_datetime(frame["available_at"], errors="coerce")
    if frame["available_at"].isna().any():
        raise ValueError("available_at must be an ISO-compatible date/time for every tail")
    return [
        Aircraft(
            tail_id=row.tail_id,
            equipment=row.equipment,
            start_station=row.start_station.upper(),
            available_at=row.available_at.to_pydatetime(),
        )
        for row in frame.itertuples(index=False)
    ]
