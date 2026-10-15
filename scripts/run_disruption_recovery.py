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
from itertools import product
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

from crew_recovery.aircraft_line_inputs import load_aircraft_line_inventory
from crew_recovery.independent_validator import pairing_groups, report_flight_id
from crew_recovery.project_paths import input_file, original_file
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
    """Find downstream flights coupled to disruptions by crew pairings or routes.

    Pairing successors take priority over same-line or merely nearby flights,
    because a retiming that repairs one crew duty can make its next connection
    infeasible. The result intentionally excludes the already-disrupted flights:
    the search chooses recovery offsets for downstream flights.
    """
    schedule_path = input_file("schedule.xlsx")
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
    delayed = set(delayed_flights)
    ordered: list[str] = []

    for flight_id in delayed_flights:
        if flight_id not in lookup:
            raise ValueError(f"Disrupted flight_id {flight_id!r} is not in the prepared schedule")

    # First collect later flights in the same supplied crew pairing. This matters
    # where retiming a middle sector can create a second downstream MGT conflict.
    pairing_report_path = input_file("pairing_report.xlsx")
    report = pd.read_excel(pairing_report_path)
    downstream_by_flight: dict[str, list[str]] = {}
    for _pairing_id, group, _splits in pairing_groups(report):
        group_ids = [report_flight_id(row) for _, row in group.iterrows()]
        for source_id in delayed_flights:
            if source_id not in group_ids:
                continue
            source = lookup[source_id]
            source_time = pd.Timestamp(source["STD"])
            for fid in group_ids:
                row = lookup.get(fid)
                if fid in delayed or row is None:
                    continue
                if not (horizon_start <= pd.Timestamp(row["Day"]).normalize() <= horizon_end):
                    continue
                if pd.Timestamp(row["STD"]) <= source_time:
                    continue
                downstream_by_flight.setdefault(source_id, []).append(fid)

    # Order each pairing's downstream options chronologically; preserve source
    # order when there are multiple explicit disruptions.
    for source_id in delayed_flights:
        candidates = sorted(
            set(downstream_by_flight.get(source_id, [])),
            key=lambda fid: (pd.Timestamp(lookup[fid]["STD"]), fid),
        )
        for fid in candidates:
            if fid not in ordered:
                ordered.append(fid)

    # If pairing successors do not provide enough candidates, use later legs on
    # the same AIRFLITE provisional line as a secondary, explicitly provisional
    # source of route-related candidates.
    _aircraft, line_assignments = load_aircraft_line_inventory(
        original_file("low_kgp"),
        input_file("schedule.xlsx"),
        start_date=horizon_start.date().isoformat(),
        end_date=horizon_end.date().isoformat(),
    )
    line_by_flight = {
        str(row["flight_id"]): row
        for row in line_assignments.to_dict("records")
    }
    for flight_id in delayed_flights:
        current = line_by_flight.get(flight_id)
        if current is None:
            continue
        same_line = line_assignments.loc[
            line_assignments["provisional_aircraft_id"].eq(current["provisional_aircraft_id"])
            & (line_assignments["STD"] > pd.Timestamp(current["STD"]))
            & line_assignments["STD"].dt.normalize().between(horizon_start, horizon_end)
        ].sort_values(["STD", "flight_id"])
        for row in same_line.to_dict("records"):
            fid = str(row["flight_id"])
            if fid not in delayed and fid not in ordered:
                ordered.append(fid)

    # Last resort: consider flights departing from the disrupted flight's
    # arrival station near the delayed arrival time.
    fallback: list[tuple[float, str]] = []
    for flight_id in delayed_flights:
        row = lookup[flight_id]
        target_station = row["To_key"]
        impacted_arrival = pd.Timestamp(row["STA"]) + pd.Timedelta(
            minutes=delay_by_flight.get(flight_id, 0)
        )
        low = pd.Timestamp(row["STA"]) - pd.Timedelta(hours=3)
        high = impacted_arrival + pd.Timedelta(hours=3)
        peers = frame.loc[
            frame["From_key"].eq(target_station)
            & ~frame["flight_id"].isin(delayed)
            & frame["Day"].dt.normalize().between(horizon_start, horizon_end)
            & frame["STD"].between(low, high)
        ].copy()
        if not peers.empty:
            peers["distance_minutes"] = (
                peers["STD"] - impacted_arrival
            ).abs().dt.total_seconds() / 60.0
            peers = peers.sort_values(["distance_minutes", "STD", "flight_id"])
            for peer in peers.to_dict("records"):
                fid = str(peer["flight_id"])
                if fid not in ordered:
                    fallback.append((float(peer["distance_minutes"]), fid))
    for _distance, fid in sorted(fallback, key=lambda item: (item[0], item[1])):
        if fid not in ordered:
            ordered.append(fid)
    return ordered

def _trial_manifest(
    base: dict[str, Any],
    offsets_by_flight: dict[str, int],
    trial_index: int,
) -> dict[str, Any]:
    """Build one trial manifest containing a joint set of downstream offsets."""
    events = [
        dict(event) for event in base["events"]
        if event.get("type") != "FLIGHT_RETIME"
    ]
    for index, (flight_id, minutes) in enumerate(sorted(offsets_by_flight.items()), start=1):
        events.append({
            "event_id": f"recovery-retime-{trial_index}-{index}",
            "type": "FLIGHT_RETIME",
            "flight_id": flight_id,
            "offset_minutes": int(minutes),
        })
    return {
        "schema_version": base["schema_version"],
        "scenario_id": base["scenario_id"],
        "description": (
            str(base.get("description", "")).rstrip()
            + " Joint bounded recovery-retiming trial; offsets are relative to disruption-adjusted times."
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
        "--require-cancellation-improvement", action="store_true",
        help="Fail unless the selected retiming combination cancels fewer flights than the all-zero retiming trial.",
    )
    parser.add_argument(
        "--retime-options",
        default="0,5,10,15",
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
    options = [value for value in options if 0 <= value <= 15]
    if 0 not in options:
        options.append(0)
    options = sorted(set(options), key=lambda value: (abs(value), value))
    if len(options) < 2:
        parser.error("At least two valid retiming tiers between 0 and 15 minutes are required.")

    candidate_flights = _discover_retiming_flights(
        delayed_flights, delay_by_flight, start_date=args.start_date, days=args.days
    )[:args.max_retimed_flights]
    if not candidate_flights:
        parser.error(
            "No downstream flights found in the disrupted flights' crew pairings, provisional routes, "
            "or nearby connection window; cannot search meaningful retiming alternatives."
        )
    combination_count = len(options) ** len(candidate_flights)
    if combination_count > 256:
        parser.error(
            f"Search would require {combination_count} full recovery runs. "
            "Reduce --max-retimed-flights or --retime-options; the safety cap is 256 combinations."
        )

    output_root = args.output_dir.expanduser().resolve()
    trial_root = output_root / "trials"
    manifest_root = output_root / "trial_manifests"
    trial_root.mkdir(parents=True, exist_ok=True)
    manifest_root.mkdir(parents=True, exist_ok=True)

    trial_records: list[dict[str, Any]] = []
    successful_trials: list[dict[str, Any]] = []
    for trial_index, offset_tuple in enumerate(
        product(options, repeat=len(candidate_flights)), start=1
    ):
        offsets_by_flight = {
            flight_id: int(offset)
            for flight_id, offset in zip(candidate_flights, offset_tuple)
        }
        manifest = _trial_manifest(base, offsets_by_flight, trial_index)
        parts = [
            f"{''.join(ch if ch.isalnum() or ch in '-_' else '_' for ch in flight_id)}_{offset:+d}"
            for flight_id, offset in sorted(offsets_by_flight.items())
        ]
        label = f"trial_{trial_index:03d}_" + "_".join(parts) + "m"
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
            "trial_index": trial_index,
            "offsets_by_flight": offsets_by_flight,
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
            successful_trials.append({
                "record": record,
                "score": score,
                "offset_magnitude": sum(abs(value) for value in offsets_by_flight.values()),
                "offset_tuple": tuple(offsets_by_flight[fid] for fid in candidate_flights),
                "output_path": out_path,
                "summary": summary,
                "manifest": manifest,
            })

    if not successful_trials:
        report = {
            "status": "FAILED_NO_SUCCESSFUL_TRIALS",
            "base_scenario_id": loaded.scenario_id,
            "start_date": args.start_date,
            "days": args.days,
            "retimed_flights_considered": candidate_flights,
            "candidate_offsets_minutes": options,
            "trials": trial_records,
            "global_optimum_claimed": False,
        }
        (output_root / "search_summary.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(report, indent=2))
        return 1

    # Pick across *joint* offset combinations, not one flight at a time. A
    # middle-flight offset can repair its predecessor connection but break the
    # next connection; evaluating the Cartesian product lets both be shifted
    # together. The all-zero combination is retained as the disruption-only
    # comparator.
    selected_trial = min(
        successful_trials,
        key=lambda trial: (
            trial["score"],
            trial["offset_magnitude"],
            trial["offset_tuple"],
        ),
    )
    selected_record = selected_trial["record"]
    selected_summary = selected_trial["summary"]
    selected_trial_path = selected_trial["output_path"]
    selected_manifest = selected_trial["manifest"]
    selected_offsets = dict(selected_trial["record"]["offsets_by_flight"])
    baseline_record = next(
        (
            trial["record"] for trial in successful_trials
            if all(value == 0 for value in trial["record"]["offsets_by_flight"].values())
        ),
        None,
    )

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
        "status": "COMPLETED_JOINT_FINITE_GRID_SEARCH",
        "method": (
            "Joint finite-grid search over downstream-flight offsets; each combination re-runs "
            "pairing validation, the recovery solver, and post-solve audits."
        ),
        "global_optimum_claimed": False,
        "ranking": [
            "minimize cancelled flights",
            "minimize total absolute selected retiming minutes among operated flights",
            "minimize the solver's partial pairing-plus-aircraft-start cost proxy",
        ],
        "candidate_offsets_minutes": options,
        "searched_flight_ids": candidate_flights,
        "selected_offsets_minutes": selected_offsets,
        "disruption_only_baseline": baseline_record,
        "selected_trial": selected_record,
        "trials": trial_records,
        "selected_output_dir": str(recovered_path),
        "limitations": [
            "Search is restricted to the configured offset grid and the downstream flights considered by --max-retimed-flights.",
            "The underlying solver still uses provisional LINE_n aircraft resources when aircraft_inventory.csv is absent.",
            "The five-term owner-approved objective and global pairing-column generation are not fully integrated.",
        ],
    }
    final_summary_path.write_text(json.dumps(final_summary, indent=2) + "\n", encoding="utf-8")
    copied_manifest = recovered_path / "scenario_manifest.json"
    copied_manifest.write_text(
        json.dumps(json.loads(final_manifest_path.read_text(encoding="utf-8")), indent=2) + "\n",
        encoding="utf-8",
    )

    retimed_count = int(
        (final_summary.get("schedule_reconciliation") or {}).get(
            "operated_flights_retimed_after_scenario", 0
        )
    )
    baseline_cancellations = (
        baseline_record.get("cancelled_flights") if baseline_record else None
    )
    selected_cancellations = final_summary.get("cancelled_flights")
    positive_retiming_ok = not args.require_positive_retiming or retimed_count >= 1
    cancellation_improvement_ok = (
        not args.require_cancellation_improvement
        or (
            baseline_cancellations is not None
            and selected_cancellations is not None
            and int(selected_cancellations) < int(baseline_cancellations)
        )
    )
    overall_status = (
        "COMPLETED_JOINT_FINITE_GRID_SEARCH"
        if positive_retiming_ok and cancellation_improvement_ok
        else "FAILED_EXPECTED_RECOVERY_IMPROVEMENT_NOT_FOUND"
    )
    report = {
        "status": overall_status,
        "base_scenario_id": loaded.scenario_id,
        "start_date": args.start_date,
        "days": args.days,
        "retimed_flights_considered": candidate_flights,
        "candidate_offsets_minutes": options,
        "selected_offsets_minutes": selected_offsets,
        "selected_score": list(_score(final_summary)),
        "selected_recovered_flights": final_summary.get("operated_flights"),
        "selected_cancelled_flights": selected_cancellations,
        "disruption_only_cancelled_flights": baseline_cancellations,
        "cancellation_improvement": (
            int(baseline_cancellations) - int(selected_cancellations)
            if baseline_cancellations is not None and selected_cancellations is not None
            else None
        ),
        "selected_retimed_flights": retimed_count,
        "recovered_schedule_dir": str(recovered_path),
        "trial_count": len(trial_records),
        "successful_trial_count": len(successful_trials),
        "trials": trial_records,
        "global_optimum_claimed": False,
    }
    (output_root / "search_summary.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2))
    return 0 if overall_status == "COMPLETED_JOINT_FINITE_GRID_SEARCH" else 1

if __name__ == "__main__":
    raise SystemExit(main())
