"""Run bounded joint crew/aircraft operate-cancel prototypes for 1W, 1M and 154D.

This produces decision-level best-effort schedules. The default uses
independently validated existing pairing-report columns; optional expanded mode
also runs the declared-domain pairing generator. Results are candidate-pool
limited and provisional when aircraft LINE_n resources are used; they are not
operational acceptance.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_one_week_joint_recovery.py"
HORIZONS = (("1W", 7), ("1M", 31), ("154D", 154))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--time-limit-seconds", type=float, default=180.0, help="MILP time limit per pass; longer horizons need more than the CI smoke-test limit.")
    parser.add_argument("--pairing-mode", choices=("existing-only", "expanded"), default="existing-only", help="existing-only uses validated report columns; expanded also runs bounded legal pairing generation and may be expensive on long horizons.")
    parser.add_argument("--output-root", type=Path, default=ROOT / "output" / "joint_recovery")
    parser.add_argument("--horizons", nargs="+", choices=[h[0] for h in HORIZONS], default=["1W", "1M", "154D"])
    parser.add_argument("--scenario-file", type=Path, default=None, help="Optional versioned JSON disruption scenario to apply to every selected horizon.")
    args = parser.parse_args()
    if args.scenario_file is not None:
        if not args.scenario_file.is_file():
            parser.error(f"Scenario file does not exist: {args.scenario_file}")
        args.scenario_file = args.scenario_file.resolve()
    args.output_root.mkdir(parents=True, exist_ok=True)
    results = []
    exit_code = 0
    for label, days in HORIZONS:
        if label not in args.horizons:
            continue
        out = args.output_root / label
        command = [
            sys.executable, str(RUNNER),
            "--start-date", "2026-10-25",
            "--days", str(days),
            "--time-limit-seconds", str(args.time_limit_seconds),
            "--output-dir", str(out),
        ]
        if args.pairing_mode == "existing-only":
            command.append("--skip-pricing")
        if args.scenario_file is not None:
            command.extend(["--scenario-file", str(args.scenario_file)])
        proc = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
        summary_path = out / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {
            "status": "RUNNER_FAILED_WITHOUT_SUMMARY",
            "return_code": proc.returncode,
            "stderr_tail": proc.stderr[-3000:],
        }
        summary["horizon_label"] = label
        summary["pairing_pool_mode"] = args.pairing_mode
        summary["run_mode"] = (
            "joint_operate_cancel_existing_validated_pairings_only"
            if args.pairing_mode == "existing-only"
            else "joint_operate_cancel_existing_plus_generated_validated_pairings"
        )
        summary["pricing_skipped_for_bounded_runtime"] = args.pairing_mode == "existing-only"
        results.append(summary)
        if proc.returncode:
            exit_code = 1
        print(json.dumps(summary, indent=2))
    aggregate = {
        "scope": ("Best-effort joint operate/cancel runs; existing validated pairing columns only." if args.pairing_mode == "existing-only" else "Best-effort joint operate/cancel runs; existing plus generated validated pairing columns within the declared pairing domain."),
        "scenario_file": str(args.scenario_file) if args.scenario_file is not None else None,
        "scenario_mode": "explicit_disruption_scenario" if args.scenario_file is not None else "baseline_no_disruption",
        "objective": [
            "hard crew/MGT, equipment, aircraft start-state, station-continuity, and route constraints",
            "maximize operated flights (equivalently minimize cancellations) before any cost trade-off",
            "then minimize supplied pairing salary plus aircraft start cost",
            "large-horizon heuristic compares the prior single-pass plans with chronological and cost-first retry plans, plus line-preferred and line-locked plans when provisional line assignments map; selects maximum coverage, then lower cost",
        ],
        "not_integrated": [
            "flight-copy retiming selection",
            "full approved operating cost",
            "crew roster/set variables",
            "globally complete pairing generation",
            "verified aircraft tail registrations",
        ],
        "horizons": [
            {
                "horizon": r.get("horizon_label"),
                "status": r.get("status"),
                "active_flights": r.get("active_flights"),
                "optimization_eligible_flights": r.get("optimization_eligible_flights"),
                "unresolved_mgt_flights": r.get("unresolved_mgt_flights", 0),
                "operated_flights": r.get("operated_flights"),
                "cancelled_flights": r.get("cancelled_flights"),
                "uncovered_or_cancelled_flights": r.get("cancelled_flights"),
                "solver_mode": r.get("solver_mode"),
                "operated_coverage_pct_of_all_flights": r.get("operated_coverage_pct_of_all_flights"),
                "flights_without_existing_pairing_candidate": r.get("flights_without_existing_pairing_candidate"),
                "operated_coverage_pct": (
                    round(100.0 * r.get("operated_flights", 0) / r["active_flights"], 2)
                    if r.get("active_flights") else None
                ),
                "selected_pairings": r.get("selected_pairings"),
                "validated_pairing_candidates": r.get("validated_pairing_candidates"),
                "aircraft_violations": len(r.get("aircraft_violations", [])),
                "crew_coverage_errors": len(r.get("crew_coverage_errors", [])),
                "operational_acceptance": r.get("operational_acceptance", False),
                "blockers": r.get("blockers", []),
                "unoperated_reason_counts_including_unresolved": r.get("unoperated_reason_counts_including_unresolved", {}),
                "heuristic_plan_comparison": r.get("heuristic_plan_comparison", {}),
            }
            for r in results
        ],
        "interpretation": (
            "1W uses the joint MILP when its input size is small. Month/full horizons use a "
            "coverage-first heuristic that compares single-pass, chronological retry, and (when available) "
            "AIRFLITE line-preserving assignment variants. The default existing-only mode uses validated "
            "supplied pairing columns; --pairing-mode "
            "expanded additionally runs the declared-domain pairing generator and may be expensive "
            "on long horizons. Reason codes describe the selected restricted plan, not global "
            "infeasibility. LINE_n resources are provisional. Flights with unresolved MGT are "
            "reported separately, not counted as cancellations. Retiming is not optimized."
        ),
    }
    (args.output_root / "horizon_summary.json").write_text(json.dumps(aggregate, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"aggregate_summary": str(args.output_root / "horizon_summary.json"), "exit_code": exit_code}, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
