#!/usr/bin/env python3
"""Bounded disruption-recovery search over explicit flight-retiming alternatives.

For each delayed flight and nearby downstream connections, try a finite grid
of retiming offsets and re-run the normal one-week recovery pipeline. Alternatives
are ranked lexicographically by cancelled flights, absolute recovery retiming,
then the solver's supported partial cost proxy. This is a bounded coordinate
search, not a global optimum or a full operational approval.
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_one_week_joint_recovery.py"
SRC = ROOT / "src"
for path in (ROOT, SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from crew_recovery.recovery_scenario import load_recovery_scenario


def _score(summary: dict[str, Any]) -> tuple[float, float, float]:
    reconciliation = summary.get("schedule_reconciliation") or {}
    cancellations = summary.get("cancelled_flights")
    if cancellations is None:
        return (math.inf, math.inf, math.inf)
    retiming = reconciliation.get("total_absolute_retiming_minutes")
    cost = summary.get("secondary_objective")
    try:
        return (
            float(cancellations),
            float(retiming) if retiming is not None else math.inf,
            float(cost) if cost is not None else math.inf,
        )
    except (TypeError, ValueError):
        return (math.inf, math.inf, math.inf)


def _discover_retiming_flights(
    delayed_flights: list[str],
    delay_by_flight: dict[str, int],
    *,
    start_date: str,
    days: int,
) -> list[str]:
    """Add nearby downstream flights whose connections can be affected by a delay."""
    schedule_path = ROOT / "input" / "schedule.xlsx"
    if not schedule_path.is_file():
        raise FileNotFoundError(
            f"Prepared schedule not found at {schedule_path}; run python setup_workspace.py first."
        )
    frame = pd.read_excel(schedule_path)
    required = {"Day", "STD", "STA", "Flt Num", "From", "To"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"Prepared schedule missing columns needed for retiming-neighbor search: {missing}")
    for column in ("Day", "STD", "STA"):
        frame[column] = pd.to_datetime(frame[column], errors="coerce")
    if frame[["Day", "STD", "STA"]].isna().any(axis=None):
        raise ValueError("Prepared schedule has invalid service dates or local times")
    frame["flight_id"] = (
        frame["Flt Num"].astype(int).astype(str)
        + "_"
        + frame["STD"].dt.strftime("%Y%m%dT%H%M%S")
    )
    frame["From_key"] = frame["From"].astype(str).str.strip().str.upper()
    frame["To_key"] = frame["To"].astype(str).str.strip().str.upper()
    lookup = {str(row["flight_id"]): row for row in frame.to_dict("records")}
    horizon_start = pd.Timestamp(start_date).normalize()
    horizon_end = horizon_start + pd.Timedelta(days=days - 1)
    ordered = list(delayed_flights)
    candidates: list[tuple[float, str]] = []
    for flight_id in delayed_flights:
        row = lookup.get(flight_id)
        if row is None:
            raise ValueError(f"Disrupted flight_id {flight_id!r} is not in the prepared schedule")
        target_station = row["To_key"]
        impacted_arrival = pd.Timestamp(row["STA"]) + pd.Timedelta(minutes=delay_by_flight.get(flight_id, 0))
        low = pd.Timestamp(row["STA"]) - pd.Timedelta(hours=3)
        high = impacted_arrival + pd.Timedelta(hours=3)
        peers = frame.loc[
            frame["From_key"].eq(target_station)
            & frame["flight_id"].ne(flight_id)
            & frame["Day"].dt.normalize().between(horizon_start, horizon_end)
            & frame["STD"].between(low, high)
        ].copy()
        if not peers.empty:
            peers["distance_minutes"] = (
                peers["STD"] - impacted_arrival
            ).abs().dt.total_seconds() / 60.0
            peers = peers.sort_values(["distance_minutes", "STD", "flight_id"])
            for peer in peers.head(2).to_dict("records"):
                fid = str(peer["flight_id"])
                if fid not in ordered:
                    candidates.append((float(peer["distance_minutes"]), fid))
    for _distance, flight_id in sorted(candidates, key=lambda item: (item[0], item[1])):
        if flight_id not in ordered:
            ordered.append(flight_id)
    return ordered


def _trial_manifest(
    base: dict[str, Any],
    fixed_offsets: dict[str, int],
    current_flight: str,
    current_offset: int,
    iteration: int,
) -> dict[str, Any]:
    events = [
        dict(event) for event in base["events"]
        if event.get("type") != "FLIGHT_RETIME"
    ]
    offsets = dict(fixed_offsets)
    offsets[current_flight] = current_offset
    for index, (flight_id, minutes) in enumerate(sorted(offsets.items()), start=1):
        events.append({
            "event_id": f"recovery-retime-{iteration}-{index}",
            "type": "FLIGHT_RETIME",
            "flight_id": flight_id,
            "offset_minutes": int(minutes),
        })
    return {
        "schema_version": base["schema_version"],
        "scenario_id": base["scenario_id"],
        "description": (
            str(base.get("description", "")).rstrip()
            + " Bounded recovery-retiming trial; offsets are relative to disruption-adjusted times."
        ).strip(),
        "events": events,
    }


def _run_trial(
    *,
    scenario_path: Path,
    output_path: Path,
    start_date: str,
    days: int,
    time_limit_seconds: float,
    pairing_mode: str,
) -> tuple[int, dict[str, Any], str]:
    command = [
        sys.executable, str(RUNNER),
        "--start-date", start_date,
        "--days", str(days),
        "--time-limit-seconds", str(time_limit_seconds),
        "--scenario-file", str(scenario_path.resolve()),
        "--output-dir", str(output_path.resolve()),
    ]
    if pairing_mode == "existing-only":
        command.append("--skip-pricing")
    proc = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    summary_path = output_path / "summary.json"
    if not summary_path.exists():
        return proc.returncode or 1, {}, (
            f"Runner did not write {summary_path}. stdout tail:\n{proc.stdout[-2000:]}\n"
            f"stderr tail:\n{proc.stderr[-2000:]}"
        )
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return 1, {}, f"Could not read runner summary: {exc}"
    if proc.returncode:
        return proc.returncode, summary, str(
            (summary.get("blockers") or [{"detail": proc.stderr[-2000:]}])[-1]
        )
    if str(summary.get("status", "")).startswith("BLOCKED"):
        return 1, summary, f"Runner returned blocked status {summary.get('status')}"
    for required_output in ("flight_decisions.csv", "schedule_reconciliation.csv", "aircraft_rotations.csv"):
        if not (output_path / required_output).is_file():
            return 1, summary, f"Missing expected recovered-schedule output: {required_output}"
    application = summary.get("scenario_application") or {}
    reconciliation = summary.get("schedule_reconciliation") or {}
    if int(application.get("flight_events_applied", 0)) < 1:
        return 1, summary, "No disruption event was applied in the trial horizon"
    if int(reconciliation.get("scenario_modified_flights", 0)) < 1:
        return 1, summary, "Schedule reconciliation did not detect the injected disruption"
    if reconciliation.get("status") != "PASS":
        return 1, summary, "Schedule reconciliation did not pass"
    return 0, summary, ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario-file", type=Path, required=True)
    parser.add_argument("--start-date", default="2026-10-25")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--time-limit-seconds", type=float, default=120.0)
    parser.add_argument("--pairing-mode", choices=("existing-only", "expanded"), default="existing-only")
    parser.add_argument("--max-retimed-flights", type=int, default=3)
    parser.add_argument(
        "--require-positive-retiming", action="store_true",
        help="Fail unless the selected recovered schedule retimes at least one operated flight; useful for end-to-end scenario tests.",
    )
    parser.add_argument(
        "--retime-options",
        default="-30,-15,0,15,30,45,60,90",
        help="Comma-separated candidate offsets (minutes) relative to the disruption-adjusted times.",
    )
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output" / "disruption_recovery")
    args = parser.parse_args()

    if args.days < 1 or args.max_retimed_flights < 1:
        parser.error("--days and --max-retimed-flights must be positive")
    scenario_path = args.scenario_file.expanduser().resolve()
    if not scenario_path.is_file():
        parser.error(f"Scenario file does not exist: {scenario_path}")
    try:
        base = json.loads(scenario_path.read_text(encoding="utf-8"))
        loaded = load_recovery_scenario(scenario_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        parser.error(f"Invalid scenario file: {exc}")
    if any(event.event_type == "FLIGHT_RETIME" for event in loaded.events):
        parser.error("Provide disruption events only; this script selects FLIGHT_RETIME offsets itself.")

    delayed_flights: list[str] = []
    delay_by_flight: dict[str, int] = {}
    for event in loaded.events:
        if event.event_type == "FLIGHT_DELAY" and event.flight_id is not None:
            if event.flight_id not in delayed_flights:
                delayed_flights.append(event.flight_id)
            delay_by_flight[event.flight_id] = delay_by_flight.get(event.flight_id, 0) + int(event.delay_minutes or 0)
    if not delayed_flights:
        parser.error("Scenario must contain at least one FLIGHT_DELAY event to search recovery-retiming alternatives.")

    try:
        options = sorted(set(int(part.strip()) for part in args.retime_options.split(",")))
    except ValueError:
        parser.error("--retime-options must be a comma-separated list of integer minutes")
    options = [value for value in options if -180 <= value <= 180]
    if 0 not in options:
        options.append(0)
    options = sorted(set(options), key=lambda value: (abs(value), value))
    if len(options) < 2:
        parser.error("At least two valid retiming options are required.")

    candidate_flights = _discover_retiming_flights(
        delayed_flights, delay_by_flight, start_date=args.start_date, days=args.days
    )[:args.max_retimed_flights]
    output_root = args.output_dir.expanduser().resolve()
    trial_root = output_root / "trials"
    manifest_root = output_root / "trial_manifests"
    trial_root.mkdir(parents=True, exist_ok=True)
    manifest_root.mkdir(parents=True, exist_ok=True)

    fixed_offsets: dict[str, int] = {}
    trial_records: list[dict[str, Any]] = []
    selected_summary: dict[str, Any] | None = None
    selected_trial_path: Path | None = None
    selected_manifest: dict[str, Any] | None = None

    for iteration, flight_id in enumerate(candidate_flights, start=1):
        candidates: list[tuple[tuple[float, float, float], int, Path, dict[str, Any], dict[str, Any]]] = []
        valid_offsets = [
            offset for offset in options
            if delay_by_flight.get(flight_id, 0) + offset >= 0
        ]
        if len(valid_offsets) < 2:
            raise RuntimeError(
                f"Fewer than two feasible retiming offsets remain for {flight_id}; "
                "each recovered departure must not precede the source schedule."
            )
        for offset in valid_offsets:
            manifest = _trial_manifest(base, fixed_offsets, flight_id, offset, iteration)
            safe_flight = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in flight_id)
            label = f"{iteration:02d}_{safe_flight}_{offset:+d}m"
            manifest_path = manifest_root / f"{label}.json"
            out_path = trial_root / label
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            rc, summary, error = _run_trial(
                scenario_path=manifest_path,
                output_path=out_path,
                start_date=args.start_date,
                days=args.days,
                time_limit_seconds=args.time_limit_seconds,
                pairing_mode=args.pairing_mode,
            )
            score = _score(summary) if rc == 0 else (math.inf, math.inf, math.inf)
            record = {
                "flight_id": flight_id,
                "offset_minutes": offset,
                "status": summary.get("status", "RUNNER_FAILED") if summary else "RUNNER_FAILED",
                "cancelled_flights": summary.get("cancelled_flights"),
                "operated_flights": summary.get("operated_flights"),
                "total_absolute_retiming_minutes": (
                    (summary.get("schedule_reconciliation") or {}).get("total_absolute_retiming_minutes")
                ),
                "secondary_objective": summary.get("secondary_objective"),
                "score_lexicographic": list(score),
                "output_dir": str(out_path),
                "error": error or None,
            }
            trial_records.append(record)
            if rc == 0:
                candidates.append((score, offset, out_path, summary, manifest))
        if not candidates:
            raise RuntimeError(
                f"Every retiming candidate failed for flight {flight_id}. "
                "Inspect trial_manifests and trial output summaries under "
                f"{output_root}."
            )
        # Cancellation count first, then absolute selected retiming, then the
        # native solver's partial cost proxy. This is only over this finite grid.
        score, offset, selected_trial_path, selected_summary, selected_manifest = min(
            candidates, key=lambda item: (item[0], abs(item[1]), item[1])
        )
        fixed_offsets[flight_id] = offset

    assert selected_summary is not None and selected_trial_path is not None and selected_manifest is not None
    recovered_path = output_root / "recovered_schedule"
    if recovered_path.exists():
        shutil.rmtree(recovered_path)
    shutil.copytree(selected_trial_path, recovered_path)

    final_manifest_path = output_root / "selected_scenario.json"
    final_manifest_path.write_text(json.dumps(selected_manifest, indent=2) + "\n", encoding="utf-8")
    final_summary_path = recovered_path / "summary.json"
    final_summary = json.loads(final_summary_path.read_text(encoding="utf-8"))
    if isinstance(final_summary.get("scenario"), dict):
        final_summary["scenario"]["source_path"] = str(final_manifest_path)
    final_summary["retiming_search"] = {
        "status": "COMPLETED_BOUNDED_COORDINATE_SEARCH",
        "method": "Sequential finite-grid search; each candidate re-runs pairing validation, the recovery solver, and post-solve audits.",
        "global_optimum_claimed": False,
        "ranking": [
            "minimize cancelled flights",
            "minimize total absolute selected retiming minutes among operated flights",
            "minimize the solver's partial pairing-plus-aircraft-start cost proxy",
        ],
        "candidate_offsets_minutes": options,
        "searched_flight_ids": candidate_flights,
        "selected_offsets_minutes": fixed_offsets,
        "trials": trial_records,
        "selected_output_dir": str(recovered_path),
        "limitations": [
            "Search is restricted to the configured offset grid and the delayed/downstream flights considered by --max-retimed-flights.",
            "The underlying solver still uses provisional LINE_n aircraft resources when aircraft_inventory.csv is absent.",
            "The five-term owner-approved objective and global pairing-column generation are not fully integrated.",
        ],
    }
    final_summary_path.write_text(json.dumps(final_summary, indent=2) + "\n", encoding="utf-8")
    copied_manifest = recovered_path / "scenario_manifest.json"
    copied_manifest.write_text(json.dumps(json.loads(final_manifest_path.read_text(encoding="utf-8")), indent=2) + "\n", encoding="utf-8")
    retimed_count = int((final_summary.get("schedule_reconciliation") or {}).get("operated_flights_retimed_after_scenario", 0))
    if args.require_positive_retiming and retimed_count < 1:
        print(json.dumps({
            "status": "FAILED_EXPECTED_RETIMING_NOT_SELECTED",
            "message": "The disruption run completed, but no operated flight was retimed. Inspect the trial scores and pairing/aircraft feasibility reports.",
            "selected_offsets_minutes": fixed_offsets,
            "recovered_schedule_dir": str(recovered_path),
        }, indent=2))
        return 1
    report = {
        "status": "COMPLETED_BOUNDED_COORDINATE_SEARCH",
        "base_scenario_id": loaded.scenario_id,
        "start_date": args.start_date,
        "days": args.days,
        "retimed_flights_considered": candidate_flights,
        "selected_offsets_minutes": fixed_offsets,
        "selected_score": list(_score(final_summary)),
        "selected_recovered_flights": final_summary.get("operated_flights"),
        "selected_cancelled_flights": final_summary.get("cancelled_flights"),
        "selected_retimed_flights": retimed_count,
        "recovered_schedule_dir": str(recovered_path),
        "trial_count": len(trial_records),
        "trials": trial_records,
        "global_optimum_claimed": False,
    }
    (output_root / "search_summary.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
