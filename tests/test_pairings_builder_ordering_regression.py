"""Regression coverage for the supplied pairing builder's ordered search.

The source-of-truth file in originals/ is intentionally unchanged by the
repository workflow. This test applies the proposed one-line patch to a
temporary copy and verifies that a later-departing, shorter flight is not
pruned just because an earlier candidate arrives after the FDP boundary.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "originals" / "pairings_builder.py"


def _load_builder_with_ordering_fix(tmp_path):
    source = SOURCE.read_text(encoding="utf-8")
    old = '        if nxt["STA"] > latest_allowed_sta:\n            break  # candidates are STD-sorted, so nothing further can qualify\n'
    new = (
        '        if nxt["STA"] > latest_allowed_sta:\n'
        '            # Candidates are sorted by STD, not STA. A later departure can\n'
        '            # still be a shorter flight and arrive before the FDP boundary.\n'
        '            continue\n'
    )
    assert source.count(old) == 1, "expected exactly one documented early-break site"
    patched = tmp_path / "pairings_builder_patched.py"
    patched.write_text(source.replace(old, new), encoding="utf-8")
    return runpy.run_path(str(patched), run_name="_pairings_builder_regression")


def _flight(fid, frm, to, departure, arrival):
    std = datetime.fromisoformat(departure)
    sta = datetime.fromisoformat(arrival)
    return {
        "flight_id": fid, "From": frm, "To": to,
        "STD": std, "STA": sta, "STD_local": std, "STA_local": sta,
        "Eqp": "320", "MGT_td": timedelta(minutes=0),
    }


def test_find_duties_continues_after_late_arriving_candidate(tmp_path):
    builder = _load_builder_with_ordering_fix(tmp_path)
    find_duties = builder["find_duties"]
    get_band = builder["get_time_band_key"]

    # Both candidates depart from KUL in STD order, but the second is shorter
    # and arrives before the two-sector FDP limit.
    first = _flight("START", "SIN", "KUL", "2026-10-25T08:00", "2026-10-25T08:30")
    long = _flight("LONG", "KUL", "BKK", "2026-10-25T09:10", "2026-10-25T13:00")
    short = _flight("SHORT", "KUL", "BKK", "2026-10-25T09:20", "2026-10-25T09:40")
    flights = [first, long, short]
    by_origin = {}
    for flight in flights:
        by_origin.setdefault(flight["From"], []).append(flight)
    for group in by_origin.values():
        group.sort(key=lambda f: f["STD"])
    stds = {station: [f["STD"] for f in group] for station, group in by_origin.items()}

    # The duty starts at 08:00. A 2-sector limit of 1.75h rejects LONG
    # (5h total) but permits SHORT (1h40m total).
    limits = {band: [1.0, 1.75, 6.0, 8.0] for band in (
        "0600-0759", "0800-1459", "1500-2159", "2200-0559"
    )}

    duties = list(find_duties(
        from_groups=by_origin,
        from_group_stds=stds,
        current_flight=first,
        duty_limits=limits,
        get_time_band_key=get_band,
        band_cache={},
        duty_start_buffer=0,
        fdp_end_buffer=0,
        duty_end_buffer=0,
        max_duty_regular=16.0,
    ))

    sequences = [tuple(f["flight_id"] for f in duty) for duty in duties]
    assert ("START", "SHORT") in sequences
    assert ("START", "LONG") not in sequences
