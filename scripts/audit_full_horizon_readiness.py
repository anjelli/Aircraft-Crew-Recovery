"""Read-only audit of full-horizon schedule and effective MGT-rule coverage.

This is a readiness check, not a recovery solver. It does not alter originals/ or
infer missing airline rules. Run with: python scripts/audit_full_horizon_readiness.py
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
import sys

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
for path in (PROJECT_ROOT, SRC_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from crew_recovery.mgt_effective import filter_effective_mgt_rules


def _date_series(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        raise ValueError(f"Missing required schedule column: {column}")
    values = pd.to_datetime(frame[column], errors="coerce")
    if values.isna().any():
        raise ValueError(f"Schedule column {column!r} has {int(values.isna().sum())} invalid dates")
    return values.dt.normalize()


def audit_schedule(schedule: pd.DataFrame) -> dict:
    """Summarize date coverage, duplicate flight instances, and equipment counts."""
    day_col = "Day" if "Day" in schedule.columns else "Date" if "Date" in schedule.columns else None
    if day_col is None:
        raise ValueError("Schedule needs a 'Day' or 'Date' column")
    days = _date_series(schedule, day_col)
    start, end = days.min(), days.max()
    expected_days = pd.date_range(start, end, freq="D")
    present_days = pd.DatetimeIndex(days.unique()).sort_values()
    missing_days = sorted(set(expected_days.date) - set(present_days.date))
    equipment_col = "Eqp" if "Eqp" in schedule.columns else None
    equipment_counts = (
        {str(k): int(v) for k, v in schedule[equipment_col].astype(str).value_counts().sort_index().items()}
        if equipment_col else {}
    )
    id_candidates = [c for c in ("flight_id", "Flight ID") if c in schedule.columns]
    duplicate_ids = int(schedule[id_candidates[0]].astype(str).duplicated().sum()) if id_candidates else None
    return {
        "rows": int(len(schedule)),
        "start_date": start.date().isoformat(),
        "end_date": end.date().isoformat(),
        "calendar_days_inclusive": int((end - start).days + 1),
        "distinct_service_dates": int(days.nunique()),
        "missing_calendar_dates": [d.isoformat() for d in missing_days],
        "equipment_column": equipment_col,
        "equipment_counts": equipment_counts,
        "duplicate_flight_ids": duplicate_ids,
        "status": "PASS" if not missing_days and (duplicate_ids in (None, 0)) else "REVIEW",
    }


def audit_mgt_rules(rules: pd.DataFrame, start_date: str, end_date: str) -> dict:
    """Count service dates with at least one effective MGT rule; fail closed on bad rules."""
    required = {"Start Date", "End Date", "Freq"}
    missing = sorted(required - set(rules.columns))
    if missing:
        return {
            "status": "BLOCKED",
            "missing_columns": missing,
            "reason": "Effective-date coverage cannot be audited without Start Date, End Date, and Freq.",
        }

    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    dates = pd.date_range(start, end, freq="D")
    uncovered = []
    active_counts = {}
    for day in dates:
        active = filter_effective_mgt_rules(rules, day)
        active_counts[day.date().isoformat()] = int(len(active))
        if active.empty:
            uncovered.append(day.date().isoformat())

    freq_values = sorted({
        str(value).strip() for value in rules["Freq"].dropna().tolist()
    })
    return {
        "status": "PASS" if not uncovered else "BLOCKED",
        "rule_rows": int(len(rules)),
        "service_date_start": start.date().isoformat(),
        "service_date_end": end.date().isoformat(),
        "service_dates_checked": int(len(dates)),
        "dates_without_effective_rules": uncovered,
        "active_rule_count_min": int(min(active_counts.values())) if active_counts else 0,
        "active_rule_count_max": int(max(active_counts.values())) if active_counts else 0,
        "freq_values_as_supplied": freq_values,
        "frequency_semantics": (
            "The current adapter interprets digits 1..7 as ISO weekdays and blank as all days. "
            "This convention requires airline-owner confirmation before operational use."
        ),
        "note": (
            "A non-empty rule set does not prove every airport/equipment combination has an applicable rule; "
            "this check only tests date/frequency activity."
        ),
    }


def run_audit(root: Path) -> dict:
    source_schedule = root / "originals" / "schedule_kgp.xlsx"
    source_mgt = root / "originals" / "MGT.xlsx"
    source_lines = root / "originals" / "low_kgp"
    missing_sources = [
        str(path.relative_to(root)) for path in (source_schedule, source_mgt, source_lines)
        if not path.is_file()
    ]
    if missing_sources:
        raise FileNotFoundError(f"Missing canonical source files: {missing_sources}")

    schedule = pd.read_excel(source_schedule)
    rules = pd.read_excel(source_mgt)
    schedule_result = audit_schedule(schedule)
    mgt_result = audit_mgt_rules(
        rules, schedule_result["start_date"], schedule_result["end_date"]
    )

    equipment = schedule_result["equipment_counts"]
    lines_text = source_lines.read_text(encoding="utf-8-sig", errors="replace")
    tail_fields = any(token in lines_text.splitlines()[0].lower() for token in ("tail", "registration", "reg no"))
    result = {
        "scope": "Read-only readiness audit; no schedule recovery is solved.",
        "canonical_inputs": {
            "schedule": "originals/schedule_kgp.xlsx",
            "mgt_rules": "originals/MGT.xlsx",
            "lines_of_work": "originals/low_kgp",
        },
        "schedule": schedule_result,
        "mgt_effective_date_coverage": mgt_result,
        "equipment_readiness": {
            "schedule_equipment_codes": sorted(equipment),
            "authoritative_equipment_crosswalk_supplied": False,
            "note": (
                "Do not infer cross-equipment compatibility from similar codes. "
                "A signed/authoritative equipment compatibility mapping is still required."
            ),
        },
        "aircraft_resource_readiness": {
            "lines_of_work_source_present": True,
            "tail_or_registration_field_detected_in_first_header_line": bool(tail_fields),
            "actual_tail_assignments_available": False,
            "note": "Line numbers are work lines, not verified aircraft registrations.",
        },
        "operational_blockers": [
            "Confirm MGT Freq weekday semantics with the airline rule owner.",
            "Resolve any service dates with no effective MGT rules; do not extrapolate rules beyond their end date.",
            "Provide an authoritative equipment/subfleet compatibility crosswalk.",
            "Confirm whether provisional lines of work may be treated as aircraft resources; they are not tail registrations.",
            "Confirm cancellation, retiming, and recovery decision rules before running an operational objective.",
        ],
    }
    if schedule_result["status"] != "PASS" or mgt_result["status"] != "PASS":
        result["readiness_status"] = "BLOCKED"
    else:
        result["readiness_status"] = "INPUT_AUDIT_PASS_WITH_OPERATIONAL_CONFIRMATIONS_PENDING"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--output", type=Path, default=None, help="Optional JSON output path; defaults to stdout only.")
    args = parser.parse_args()
    report = run_audit(args.repo_root)
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
