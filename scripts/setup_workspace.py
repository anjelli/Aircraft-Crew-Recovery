#!/usr/bin/env python3
"""Validate source inputs, then create canonical prepared files under input/.

Source artifacts stay in originals/. This script deliberately does not copy source files
into the project root, where stale duplicate versions could shadow the canonical files.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ORIGINALS = ROOT / "originals"
REQUIRED_ORIGINALS = (
    "MGT.py",
    "MGT.xlsx",
    "cost_parameters.csv",
    "fdp_rules.csv",
    "low_kgp",
    "pairing_report.xlsx",
    "pairings_builder.py",
    "revenue.xlsx",
    "schedule_kgp.xlsx",
)


def main() -> int:
    missing = [name for name in REQUIRED_ORIGINALS if not (ORIGINALS / name).is_file()]
    if missing:
        print(f"Missing required source files in {ORIGINALS}: {missing}", file=sys.stderr)
        return 1

    subprocess.check_call([sys.executable, str(ROOT / "scripts" / "prep_inputs.py")])
    print("Workspace ready. Experiments read source files from originals/ and prepared files from input/.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
