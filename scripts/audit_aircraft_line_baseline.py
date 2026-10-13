#!/usr/bin/env python3
"""Audit the supplied AIRFLITE aircraft line plan against the current MGT rules."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for entry in (ROOT, SRC):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from crew_recovery.aircraft_line_audit import audit_aircraft_line_transitions
from crew_recovery.aircraft_line_inputs import load_aircraft_line_inventory
from crew_recovery.independent_validator import MGTResolver, prepare_schedule
from crew_recovery.project_paths import input_file, original_file


def load_canonical_schedule(path: Path) -> pd.DataFrame:
    frame = pd.read_excel(path)
    required = {
        "Day", "STD", "STA", "STD_sin", "STA_sin",
        "Flt Num", "From", "To", "Eqp", "Arvl Ctry",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"Canonical schedule is missing columns: {missing}")
    frame = frame.copy()
    for column in ("Day", "STD", "STA", "STD_sin", "STA_sin"):
        frame[column] = pd.to_datetime(frame[column], errors="coerce")
    if frame[["Day", "STD", "STA", "STD_sin", "STA_sin"]].isna().any(axis=None):
        raise ValueError("Canonical schedule contains invalid date/time values")
    return frame.drop_duplicates(
        subset=["Day", "STD", "STA", "Eqp", "Flt Num", "From", "To"]
    ).reset_index(drop=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", default="2026-10-25")
    parser.add_argument("--end-date", default="2027-03-27")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output" / "audit")
    args = parser.parse_args()
    if pd.Timestamp(args.end_date) < pd.Timestamp(args.start_date):
        parser.error("--end-date must not precede --start-date")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    raw_schedule = load_canonical_schedule(input_file("schedule.xlsx"))
    resolver = MGTResolver(original_file("MGT.xlsx"))
    prepared = prepare_schedule(raw_schedule, resolver)
    _aircraft, line_assignments = load_aircraft_line_inventory(
        original_file("low_kgp"),
        input_file("schedule.xlsx"),
        start_date=args.start_date,
        end_date=args.end_date,
    )
    transitions, summary = audit_aircraft_line_transitions(line_assignments, prepared)
    output_summary = {
        "scope": {
            "start_date": args.start_date,
            "end_date": args.end_date,
        },
        "source": "originals/low_kgp reconciled to input/schedule.xlsx and originals/MGT.xlsx",
        "prepared_schedule_flights": int(len(prepared)),
        "line_assignment_reconciliation": "PASS",
        "unresolved_mgt_flights_in_scope": int(
            prepared.loc[
                pd.to_datetime(prepared["Day"]).dt.normalize().between(
                    pd.Timestamp(args.start_date), pd.Timestamp(args.end_date)
                ),
                "mgt_status",
            ].astype(str).str.contains(
                "_CONFLICT|MISSING|OUT_OF_EFFECTIVE_DATE", regex=True
            ).sum()
        ),
        **summary,
    }
    transitions.to_csv(args.output_dir / "aircraft_line_baseline_transitions.csv", index=False)
    (args.output_dir / "aircraft_line_baseline_summary.json").write_text(
        json.dumps(output_summary, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(output_summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
