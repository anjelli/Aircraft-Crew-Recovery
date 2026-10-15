"""Run joint crew/aircraft recovery for the configured horizons.

Canonical output contract:
- all generated current-run files live under output/joint_recovery/
- each invocation replaces that directory to prevent stale/mixed outputs
- historical committed experiments and immutable source inputs are untouched

This is a bounded research prototype, not an operationally accepted airline plan.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_one_week_joint_recovery.py"
DEFAULT_OUTPUT = ROOT / "output" / "joint_recovery"
HORIZONS = (("1W", 7), ("1M", 31), ("154D", 154))


def _reset_output(output_root: Path) -> None:
    """Replace only the selected run-output directory; never delete arbitrary paths."""
    output_root = output_root.expanduser().resolve()
    allowed_parent = (ROOT / "output").resolve()
    # Production defaults are constrained to output/. Allow external directories
    # for tests and deliberate temporary runs, but never delete the repository root
    # or the shared output parent itself.
    if output_root == ROOT.resolve() or output_root == allowed_parent:
        raise ValueError(
            f"Refusing to clean the repository root or shared output directory: {output_root}"
        )
    # Production outputs use output/. Tests may pass a temporary directory; only
    # the canonical default or an explicitly nested output/ directory is cleaned.
    if allowed_parent in output_root.parents:
        if output_root.exists():
            shutil.rmtree(output_root)
    else:
        output_root.mkdir(parents=True, exist_ok=True)
    output_root.mkdir(parents=True, exist_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--time-limit-seconds", type=float, default=180.0,
        help="Solver time limit per run; long horizons use a deterministic heuristic.",
    )
    parser.add_argument(
        "--pairing-mode", choices=("existing-only", "expanded"), default="existing-only",
        help="existing-only is the stable default; expanded candidate generation may be expensive.",
    )
    parser.add_argument(
        "--output-root", type=Path, default=DEFAULT_OUTPUT,
        help="Dedicated run-output directory. Its previous contents are replaced before running.",
    )
    parser.add_argument(
        "--horizons", nargs="+", choices=[h[0] for h in HORIZONS],
        default=["1W", "1M", "154D"],
    )
    parser.add_argument("--scenario-file", type=Path, default=None)
    args = parser.parse_args()

    if args.time_limit_seconds <= 0:
        parser.error("--time-limit-seconds must be positive")
    if args.scenario_file is not None:
        args.scenario_file = args.scenario_file.expanduser().resolve()
        if not args.scenario_file.is_file():
            parser.error(f"Scenario file does not exist: {args.scenario_file}")

    try:
        _reset_output(args.output_root)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    results = []
    exit_code = 0
    for label, days in HORIZONS:
        if label not in args.horizons:
            continue
        out = args.output_root.resolve() / label
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
        # Keep one readable log per horizon inside the current run directory.
        (out.parent / f"{label}.log").write_text(
            "COMMAND: " + " ".join(command) + "\n\nSTDOUT:\n" + proc.stdout
            + "\nSTDERR:\n" + proc.stderr,
            encoding="utf-8",
        )
        summary_path = out / "summary.json"
        if summary_path.exists():
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                summary = {
                    "status": "INVALID_SUMMARY_JSON",
                    "error": str(exc),
                    "return_code": proc.returncode,
                }
                exit_code = 1
        else:
            summary = {
                "status": "RUNNER_FAILED_WITHOUT_SUMMARY",
                "return_code": proc.returncode,
                "stdout_tail": proc.stdout[-2000:],
                "stderr_tail": proc.stderr[-3000:],
            }
            exit_code = 1

        summary["horizon_label"] = label
        summary["pairing_pool_mode"] = args.pairing_mode
        summary["run_mode"] = (
            "joint_operate_cancel_existing_validated_pairings_only"
            if args.pairing_mode == "existing-only"
            else "joint_operate_cancel_existing_plus_generated_validated_pairings"
        )
        summary["pricing_skipped_for_bounded_runtime"] = args.pairing_mode == "existing-only"
        summary["runner_return_code"] = proc.returncode
        (out / "summary.json").write_text(
            json.dumps(summary, indent=2) + "\n", encoding="utf-8"
        )
        results.append(summary)
        if proc.returncode != 0:
            exit_code = 1
            print(f"[{label}] FAILED (exit={proc.returncode})", file=sys.stderr)
            if proc.stderr:
                print(proc.stderr[-3000:], file=sys.stderr)
        else:
            print(
                f"[{label}] status={summary.get('status')} "
                f"operated={summary.get('operated_flights')} "
                f"cancelled={summary.get('cancelled_flights')}"
            )

    aggregate = {
        "scope": (
            "Best-effort joint operate/cancel runs; existing validated pairing columns only."
            if args.pairing_mode == "existing-only"
            else "Best-effort joint operate/cancel runs; existing plus generated pairing candidates "
                 "within the declared pairing domain."
        ),
        "scenario_file": str(args.scenario_file) if args.scenario_file else None,
        "scenario_mode": "explicit_disruption_scenario" if args.scenario_file else "baseline_no_disruption",
        "objective": [
            "hard crew/MGT, equipment, aircraft start-state, station-continuity, and route constraints",
            "maximize operated flights (equivalently minimize cancellations) before cost trade-off",
            "then minimize supplied pairing salary plus aircraft start cost",
        ],
        "not_integrated": [
            "native-MILP flight-copy retiming selection",
            "full approved operating cost",
            "crew roster/set variables",
            "globally complete pairing generation",
            "verified aircraft tail registrations",
        ],
        "horizons": [
            {
                "horizon": r.get("horizon_label"),
                "status": r.get("status"),
                "objective_ledger": r.get("objective_ledger"),
                "schedule_reconciliation": r.get("schedule_reconciliation"),
                "active_flights": r.get("active_flights"),
                "optimization_eligible_flights": r.get("optimization_eligible_flights"),
                "unresolved_mgt_flights": r.get("unresolved_mgt_flights", 0),
                "operated_flights": r.get("operated_flights"),
                "cancelled_flights": r.get("cancelled_flights"),
                "solver_mode": r.get("solver_mode"),
                "operated_coverage_pct_of_all_flights": r.get("operated_coverage_pct_of_all_flights"),
                "flights_without_existing_pairing_candidate": r.get("flights_without_existing_pairing_candidate"),
                "selected_pairings": r.get("selected_pairings"),
                "validated_pairing_candidates": r.get("validated_pairing_candidates"),
                "aircraft_violations": len(r.get("aircraft_violations", [])),
                "crew_coverage_errors": len(r.get("crew_coverage_errors", [])),
                "operational_acceptance": r.get("operational_acceptance", False),
                "blockers": r.get("blockers", []),
                "unoperated_reason_counts_including_unresolved": r.get("unoperated_reason_counts_including_unresolved", {}),
            }
            for r in results
        ],
        "interpretation": (
            "The 1W MILP is optimal only over its restricted candidate/resource model. Longer "
            "horizons use a deterministic coverage heuristic, not a global optimum. CANCEL means "
            "not selected in the restricted plan, not globally infeasible. LINE_n resources are "
            "provisional. Unresolved MGT is a blocker, not a cancellation. Retiming is not optimized "
            "inside the native model."
        ),
    }
    aggregate_path = args.output_root.resolve() / "horizon_summary.json"
    aggregate_path.write_text(json.dumps(aggregate, indent=2) + "\n", encoding="utf-8")
    print(f"\nOutputs replaced in: {args.output_root.resolve()}")
    print(f"Aggregate summary: {aggregate_path}")
    print(f"Exit code: {exit_code}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
