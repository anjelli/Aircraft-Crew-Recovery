"""Explicit flight-copy and station-time network primitives for retiming.

This module generates copies only; it does not select copies or alter the
operate/cancel MILP. The caller must provide approved offsets and resolved MGT.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Mapping, Sequence


@dataclass(frozen=True)
class FlightCopy:
    copy_id: str
    flight_id: str
    offset_minutes: int
    origin: str
    destination: str
    equipment: str
    std: object
    sta: object
    mgt_min: float


def generate_flight_copies(
    flights: Sequence[Mapping[str, object]],
    *,
    allowed_offsets_minutes: Sequence[int] = (0, 5, 10, 15),
) -> tuple[FlightCopy, ...]:
    """Create time-shifted copies while preserving each flight's block duration."""
    offsets = tuple(int(x) for x in allowed_offsets_minutes)
    if not offsets or len(offsets) != len(set(offsets)) or 0 not in offsets:
        raise ValueError("allowed offsets must be unique and include zero")
    if any(x < 0 for x in offsets):
        raise ValueError("negative retiming offsets are not enabled by current source cost rules")
    copies = []
    seen = set()
    for flight in flights:
        fid = str(flight.get("flight_id", "")).strip()
        if not fid or fid in seen:
            raise ValueError("flight IDs must be unique and non-empty")
        seen.add(fid)
        std, sta = flight.get("STD"), flight.get("STA")
        if std is None or sta is None or sta < std:
            raise ValueError(f"Invalid STD/STA for flight {fid!r}")
        mgt_raw = flight.get("mgt_min")
        if mgt_raw is None:
            raise ValueError(f"Missing resolved MGT for flight {fid!r}")
        mgt = float(mgt_raw)
        if not isfinite(mgt) or mgt < 0:
            raise ValueError(f"Invalid MGT for flight {fid!r}")
        for offset in offsets:
            delta = __import__("datetime").timedelta(minutes=offset)
            copies.append(FlightCopy(
                copy_id=f"{fid}__D{offset:02d}",
                flight_id=fid,
                offset_minutes=offset,
                origin=str(flight["From"]).strip().upper(),
                destination=str(flight["To"]).strip().upper(),
                equipment=str(flight["Eqp"]).strip(),
                std=std + delta,
                sta=sta + delta,
                mgt_min=mgt,
            ))
    return tuple(copies)


def feasible_copy_connection(first: FlightCopy, second: FlightCopy) -> bool:
    """Check aircraft station/equipment/MGT feasibility between two flight copies."""
    return (
        first.copy_id != second.copy_id
        and first.flight_id != second.flight_id
        and first.destination == second.origin
        and first.equipment == second.equipment
        and second.std >= first.sta
            + __import__("datetime").timedelta(minutes=first.mgt_min)
    )


def build_copy_network(copies: Sequence[FlightCopy]) -> tuple[tuple[str, str], ...]:
    """Return every feasible forward-in-time copy arc; no heuristic pruning."""
    ordered = sorted(copies, key=lambda item: (item.std, item.copy_id))
    arcs = []
    for i, first in enumerate(ordered):
        for second in ordered[i + 1:]:
            if second.std < first.sta:
                continue
            if feasible_copy_connection(first, second):
                arcs.append((first.copy_id, second.copy_id))
    return tuple(arcs)
