#!/usr/bin/env python3
"""Build canonical prepared inputs without modifying files under originals/."""
from __future__ import annotations

import shutil
from pathlib import Path

import pandas as pd

from project_paths import INPUT_DIR, ORIGINALS_DIR, original_file

ROOT = Path(__file__).resolve().parent
AUDIT_DIR = ROOT / "output" / "audit"


def prepare_inputs() -> tuple[Path, Path]:
    source_schedule = original_file("schedule_kgp.xlsx")
    source_report = original_file("pairing_report.xlsx")
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)

    schedule = pd.read_excel(source_schedule)
    # Some exports include an Excel index column. It is not a flight attribute and must
    # not prevent otherwise identical rows from being identified as duplicates.
    dedupe_columns = [column for column in schedule.columns if column != "Unnamed: 0"]
    if not dedupe_columns:
        raise ValueError("The source schedule has no usable columns for duplicate detection.")

    duplicate_mask = schedule.duplicated(subset=dedupe_columns, keep="first")
    duplicate_rows_path = AUDIT_DIR / "schedule_exact_duplicate_rows.csv"
    schedule.loc[schedule.duplicated(subset=dedupe_columns, keep=False)].to_csv(
        duplicate_rows_path, index=False
    )

    prepared_schedule = INPUT_DIR / "schedule.xlsx"
    schedule.loc[~duplicate_mask].to_excel(prepared_schedule, index=False)
    prepared_report = INPUT_DIR / "pairing_report.xlsx"
    shutil.copy2(source_report, prepared_report)

    print(
        f"Prepared schedule: {len(schedule):,} source rows; "
        f"{int(duplicate_mask.sum()):,} exact duplicate rows removed; "
        f"{len(schedule) - int(duplicate_mask.sum()):,} rows retained."
    )
    print(f"Duplicate rows logged to: {duplicate_rows_path}")
    print(f"Prepared inputs written to: {INPUT_DIR}")
    return prepared_schedule, prepared_report


if __name__ == "__main__":
    prepare_inputs()
