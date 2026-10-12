from __future__ import annotations

from pathlib import Path
import sys

import pandas as pd

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from scripts.audit_full_horizon_readiness import audit_mgt_rules, audit_schedule


def test_schedule_audit_reports_missing_calendar_date_and_equipment():
    frame = pd.DataFrame({
        "Day": ["2026-10-25", "2026-10-27"],
        "Eqp": ["320", "32Q"],
        "flight_id": ["F1", "F2"],
    })
    result = audit_schedule(frame)
    assert result["calendar_days_inclusive"] == 3
    assert result["missing_calendar_dates"] == ["2026-10-26"]
    assert result["equipment_counts"] == {"320": 1, "32Q": 1}
    assert result["status"] == "REVIEW"


def test_mgt_audit_flags_dates_without_effective_rules():
    rules = pd.DataFrame({
        "Start Date": ["2026-10-25"],
        "End Date": ["2026-10-26"],
        "Freq": ["1234567"],
    })
    result = audit_mgt_rules(rules, "2026-10-25", "2026-10-27")
    assert result["status"] == "BLOCKED"
    assert result["dates_without_effective_rules"] == ["2026-10-27"]


def test_mgt_audit_blocks_when_effective_columns_are_missing():
    result = audit_mgt_rules(pd.DataFrame({"Freq": ["1234567"]}), "2026-10-25", "2026-10-26")
    assert result["status"] == "BLOCKED"
    assert result["missing_columns"] == ["End Date", "Start Date"]
