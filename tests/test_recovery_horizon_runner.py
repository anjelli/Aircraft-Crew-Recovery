from __future__ import annotations

from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from run_recovery_horizon import _canonical_schedule, _horizon_frame, export_horizon


def _schedule(days=155):
    start = pd.Timestamp("2026-10-25")
    rows = []
    for i in range(days):
        day = start + pd.Timedelta(days=i)
        rows.append({
            "flight_id": f"F{i:03d}",
            "service_date": day,
            "STD": day + pd.Timedelta(hours=8),
            "STA": day + pd.Timedelta(hours=10),
            "From": "SIN",
            "To": "KUL",
            "Eqp": "320",
            "Flt Num": i,
        })
    return pd.DataFrame(rows)


def test_horizon_runner_selects_exact_7_31_and_155_day_windows():
    schedule = _schedule()
    assert len(_horizon_frame(schedule, "1W")) == 7
    assert len(_horizon_frame(schedule, "1M")) == 31
    assert len(_horizon_frame(schedule, "155D")) == 155


def test_horizon_export_is_explicitly_blocked_and_exports_all_deliverables(tmp_path):
    schedule = _schedule(7)
    exceptions = [{
        "exception_id": "TAILS_MISSING",
        "severity": "BLOCKER",
        "scope": "aircraft_rotation",
        "detail": "Actual tails unavailable.",
        "resolution": "Supply authoritative tail inventory.",
    }]
    readiness = {"actual_tail_inventory_available": False}
    summary = export_horizon(schedule, "1W", tmp_path, exceptions, readiness)

    assert summary["status"] == "BLOCKED_NOT_OPTIMIZED"
    assert summary["optimized_flights"] == 0
    assert summary["rolling_windows_used"] is False
    assert (tmp_path / "recovery_1W.xlsx").is_file()
    for name in summary["machine_readable_files"]:
        assert (tmp_path / name).is_file()
    flight_plan = pd.read_csv(tmp_path / "flight_level_recovery.csv")
    assert set(flight_plan["recovery_action"]) == {"UNRESOLVED"}
    assert set(flight_plan["recovery_status"]) == {"BLOCKED_NOT_OPTIMIZED"}


def test_canonical_schedule_drops_exact_duplicate_rows(tmp_path):
    schedule = pd.DataFrame([
        {
            "Day": "2026-10-25",
            "STD": "2026-10-25 18:55:00",
            "STA": "2026-10-25 22:55:00",
            "Eqp": "32N",
            "Flt Num": 905,
            "From": "MFM",
            "To": "SIN",
        },
        {
            "Day": "2026-10-25",
            "STD": "2026-10-25 18:55:00",
            "STA": "2026-10-25 22:55:00",
            "Eqp": "32N",
            "Flt Num": 905,
            "From": "MFM",
            "To": "SIN",
        },
    ])
    schedule_path = tmp_path / "schedule.xlsx"
    schedule.to_excel(schedule_path, index=False)

    canonical = _canonical_schedule(schedule_path)
    assert len(canonical) == 1
    assert canonical["flight_id"].nunique() == 1
