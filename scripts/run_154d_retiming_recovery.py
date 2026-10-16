#!/usr/bin/env python3
"""Run retiming recovery directly over the complete 154-day planning horizon.

Run the bounded retiming search over the complete 154-day planning horizon.
The default uses expanded pairing generation so each trial can validate new
pairing candidates against its retimed schedule. This is still a finite-grid
search, not global continuous-time optimization; aircraft resources remain
provisional unless verified tail inventory is supplied.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEARCH = ROOT / "scripts" / "run_disruption_recovery.py"
DEFAULT_SCENARIO = ROOT / "scenarios" / "demo_disruption_1w.json"
DEFAULT_OUTPUT = ROOT / "output" / "recovery_154d_retiming"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario-file", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument("--time-limit-seconds", type=float, default=180.0)
    parser.add_argument(
        "--pairing-mode",
        choices=("existing-only", "expanded"),
        default="expanded",
        help=(
            "expanded regenerates and validates pairing candidates against each "
            "retimed schedule; existing-only is a bounded diagnostic mode and can "
            "leave disrupted flights with zero pairing candidates."
        ),
    )
    parser.add_argument("--aircraft-solver", choices=("auto", "flow"), default="auto")
    parser.add_argument("--max-retimed-flights", type=int, default=2)
    parser.add_argument("--retime-options", default="0,5",
                        help="Comma-separated recovery offsets in minutes; each must be 0..15.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--require-positive-retiming", action="store_true")
    parser.add_argument("--require-cancellation-improvement", action="store_true")
    args = parser.parse_args()

    scenario = args.scenario_file.expanduser().resolve()
    if not scenario.is_file():
        parser.error(f"Scenario file does not exist: {scenario}")
    if args.time_limit_seconds <= 0:
        parser.error("--time-limit-seconds must be positive")
    if args.max_retimed_flights < 1:
        parser.error("--max-retimed-flights must be positive")

    output = args.output_dir.expanduser().resolve()
    allowed_parent = (ROOT / "output").resolve()
    if output == ROOT.resolve() or output == allowed_parent or allowed_parent not in output.parents:
        parser.error(f"--output-dir must be a dedicated directory below {allowed_parent}")

    command = [
        sys.executable, str(SEARCH),
        "--scenario-file", str(scenario),
        "--start-date", "2026-10-25",
        "--days", "154",
        "--time-limit-seconds", str(args.time_limit_seconds),
        "--pairing-mode", args.pairing_mode,
        "--aircraft-solver", args.aircraft_solver,
        "--max-retimed-flights", str(args.max_retimed_flights),
        "--retime-options", args.retime_options,
        "--output-dir", str(output),
    ]
    if args.require_positive_retiming:
        command.append("--require-positive-retiming")
    if args.require_cancellation_improvement:
        command.append("--require-cancellation-improvement")

    print("Planning horizon: 2026-10-25 through 2027-03-27 (154 days)")
    print("Executing:", " ".join(command))
    return subprocess.run(command, cwd=ROOT).returncode


if __name__ == "__main__":
    raise SystemExit(main())