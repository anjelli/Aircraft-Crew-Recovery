"""Horizon-wide recovery preflight and deliverable exporter.

The runner deliberately fails closed: it never labels the source schedule as
recovered when required operational inputs or an integrated crew solver are
missing. It exports one workbook plus CSV/JSON artifacts for 1W, 1M, and 155D.
No rolling windows are used, so no inter-window state is silently discarded.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for p in (ROOT, SRC):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from crew_recovery.mgt_effective import filter_effective_mgt_rules


HORIZONS = {"1W": 7, "1M": 31, "155D": 155}


def _canonical_schedule(path: Path) -> pd.DataFrame:
    frame = pd.read_excel(path)
    date_col = "Day" if "Day" in frame.columns else "Date" if "Date" in frame.columns else None
    if date_col is None:
        raise ValueError("Canonical schedule requires Day or Date")
    for col in (date_col, "STD", "STA", "Eqp", "Flt Num", "From", "To"):
        if col not in frame.columns:
            raise ValueError(f"Canonical schedule is missing required column {col!r}")
    frame = frame.copy()
    frame["service_date"] = pd.to_datetime(frame[date_col], errors="coerce").dt.normalize()
    frame["STD"] = pd.to_datetime(frame["STD"], errors="coerce")
    frame["STA"] = pd.to_datetime(frame["STA"], errors="coerce")
    invalid = frame[["service_date", "STD", "STA"]].isna().any(axis=1)
    if invalid.any():
        raise ValueError(f"Canonical schedule has {int(invalid.sum())} invalid date/time rows")
    frame["flight_id"] = (
        frame["Flt Num"].astype(str).str.replace(r"\.0$", "", regex=True)
        + "_" + frame["STD"].dt.strftime("%Y%m%dT%H%M%S")
    )
    if frame["flight_id"].duplicated().any():
        raise ValueError("Canonical schedule produces duplicate flight IDs")
    return frame.sort_values(["STD", "flight_id"]).reset_index(drop=True)


def _readiness(schedule: pd.DataFrame, mgt_path: Path, lines_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    exceptions: list[dict[str, Any]] = []
    rules = pd.read_excel(mgt_path)
    start = schedule["service_date"].min()
    end = schedule["service_date"].max()
    missing_rule_dates = []
    for day in pd.date_range(start, end, freq="D"):
        if filter_effective_mgt_rules(rules, day).empty:
            missing_rule_dates.append(day.date().isoformat())
    if missing_rule_dates:
        exceptions.append({
            "exception_id": "MGT_EFFECTIVE_DATE_GAP",
            "severity": "BLOCKER",
            "scope": "full_horizon",
            "detail": "No effective MGT rules found for service dates: " + ", ".join(missing_rule_dates),
            "resolution": "Obtain and validate the authoritative effective MGT rules; do not extrapolate.",
        })

    exceptions.append({
        "exception_id": "ACTUAL_AIRCRAFT_TAIL_INVENTORY_MISSING",
        "severity": "BLOCKER",
        "scope": "aircraft_rotation",
        "detail": "The supplied lines-of-work source does not establish actual aircraft registration/tail identities and initial station/availability.",
        "resolution": "Supply an owner-approved aircraft inventory with tail ID, equipment, start station, and availability timestamp.",
    })
    exceptions.append({
        "exception_id": "EQUIPMENT_COMPATIBILITY_CROSSWALK_MISSING",
        "severity": "BLOCKER",
        "scope": "aircraft_rotation",
        "detail": "No authoritative equipment compatibility crosswalk is available; codes such as 32P/32Q must not be inferred compatible.",
        "resolution": "Supply an authoritative tail/equipment compatibility mapping.",
    })
    exceptions.append({
        "exception_id": "END_TO_END_CREW_RECOVERY_NOT_INTEGRATED",
        "severity": "BLOCKER",
        "scope": "crew_pairings_and_recovery",
        "detail": "The repository has a restricted-master integer layer and pricing prototype, but no integrated solver that jointly selects validated crew pairings and aircraft rotations with cancellation/retiming decisions.",
        "resolution": "Integrate validated pairing columns, explicit retimed options, cancellation costs, and aircraft route-flow constraints; verify against exhaustive tiny instances.",
    })
    return exceptions, {
        "schedule_start": start.date().isoformat(),
        "schedule_end": end.date().isoformat(),
        "schedule_rows": int(len(schedule)),
        "mgt_dates_without_effective_rules": missing_rule_dates,
        "aircraft_lines_source_present": lines_path.is_file(),
        "actual_tail_inventory_available": False,
        "equipment_crosswalk_available": False,
        "integrated_crew_recovery_available": False,
    }


def _horizon_frame(schedule: pd.DataFrame, horizon: str) -> pd.DataFrame:
    if horizon not in HORIZONS:
        raise ValueError(f"Unsupported horizon {horizon!r}; choose one of {sorted(HORIZONS)}")
    start = schedule["service_date"].min()
    end = start + pd.Timedelta(days=HORIZONS[horizon] - 1)
    return schedule.loc[schedule["service_date"].between(start, end)].copy().reset_index(drop=True)


def export_horizon(schedule: pd.DataFrame, horizon: str, output_dir: Path, exceptions: list[dict[str, Any]], readiness: dict[str, Any]) -> dict[str, Any]:
    selected = _horizon_frame(schedule, horizon)
    scoped_exceptions = [dict(e, horizon=horizon) for e in exceptions]
    blocked = any(e["severity"] == "BLOCKER" for e in scoped_exceptions)
    flight_plan = selected[[
        "flight_id", "service_date", "STD", "STA", "From", "To", "Eqp", "Flt Num"
    ]].copy()
    flight_plan["recovery_action"] = "UNRESOLVED"
    flight_plan["assigned_pairing_id"] = pd.NA
    flight_plan["assigned_aircraft_tail"] = pd.NA
    flight_plan["retime_minutes"] = pd.NA
    flight_plan["recovery_status"] = "BLOCKED_NOT_OPTIMIZED" if blocked else "READY_NOT_OPTIMIZED"

    pairings = pd.DataFrame(columns=["pairing_id", "flight_ids", "duty_splits", "validation_status"])
    rotations = pd.DataFrame(columns=["tail_id", "sequence", "flight_id", "STD", "STA", "From", "To"])
    cancellations = pd.DataFrame(columns=["flight_id", "cancellation_cost", "reason"])
    retimings = pd.DataFrame(columns=["flight_id", "original_STD", "recovered_STD", "retime_minutes", "retime_cost"])
    validation = pd.DataFrame([
        {"check": "horizon_inputs_ready", "status": "BLOCKED" if blocked else "READY", "detail": "See unresolved exceptions and readiness JSON."},
        {"check": "crew_pairing_solution", "status": "NOT_RUN", "detail": "No integrated end-to-end recovery solution was produced."},
        {"check": "aircraft_rotation_solution", "status": "NOT_RUN", "detail": "No authoritative tail inventory and initial state were supplied."},
        {"check": "cancellation_and_retiming_decisions", "status": "NOT_RUN", "detail": "No recovery decisions are fabricated by this preflight runner."},
    ])
    exceptions_frame = pd.DataFrame(scoped_exceptions, columns=["exception_id", "severity", "scope", "detail", "resolution", "horizon"])

    output_dir.mkdir(parents=True, exist_ok=True)
    flight_plan.to_csv(output_dir / "flight_level_recovery.csv", index=False)
    pairings.to_csv(output_dir / "crew_pairings.csv", index=False)
    rotations.to_csv(output_dir / "aircraft_rotations.csv", index=False)
    cancellations.to_csv(output_dir / "cancellations.csv", index=False)
    retimings.to_csv(output_dir / "retimings.csv", index=False)
    validation.to_csv(output_dir / "validation_results.csv", index=False)
    exceptions_frame.to_csv(output_dir / "unresolved_exceptions.csv", index=False)

    workbook = output_dir / f"recovery_{horizon}.xlsx"
    with pd.ExcelWriter(workbook, engine="openpyxl") as writer:
        flight_plan.to_excel(writer, sheet_name="Flight Recovery", index=False)
        pairings.to_excel(writer, sheet_name="Crew Pairings", index=False)
        rotations.to_excel(writer, sheet_name="Aircraft Rotations", index=False)
        cancellations.to_excel(writer, sheet_name="Cancellations", index=False)
        retimings.to_excel(writer, sheet_name="Retimings", index=False)
        validation.to_excel(writer, sheet_name="Validation", index=False)
        exceptions_frame.to_excel(writer, sheet_name="Unresolved Exceptions", index=False)
    summary = {
        "horizon": horizon,
        "status": "BLOCKED_NOT_OPTIMIZED" if blocked else "READY_NOT_OPTIMIZED",
        "service_date_start": selected["service_date"].min().date().isoformat(),
        "service_date_end": selected["service_date"].max().date().isoformat(),
        "scheduled_flights": int(len(selected)),
        "optimized_flights": 0,
        "selected_pairings": 0,
        "aircraft_rotations": 0,
        "cancellations_decided": 0,
        "retimings_decided": 0,
        "unresolved_blockers": int(sum(e["severity"] == "BLOCKER" for e in scoped_exceptions)),
        "rolling_windows_used": False,
        "state_carried_between_windows": None,
        "workbook": str(workbook),
        "machine_readable_files": [
            "flight_level_recovery.csv", "crew_pairings.csv", "aircraft_rotations.csv",
            "cancellations.csv", "retimings.csv", "validation_results.csv", "unresolved_exceptions.csv",
        ],
        "readiness": readiness,
        "interpretation": "This is an audit/export package, not a recovered schedule. UNRESOLVED means no optimizer decision was made.",
    }
    (output_dir / "recovery_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--horizons", nargs="+", choices=sorted(HORIZONS), default=["1W", "1M", "155D"])
    parser.add_argument("--schedule", default=str(ROOT / "originals" / "schedule_kgp.xlsx"))
    parser.add_argument("--mgt", default=str(ROOT / "originals" / "MGT.xlsx"))
    parser.add_argument("--aircraft-lines", default=str(ROOT / "originals" / "low_kgp"))
    parser.add_argument("--output-root", default=str(ROOT / "output" / "recovery"))
    args = parser.parse_args()

    schedule = _canonical_schedule(Path(args.schedule))
    results = []
    for horizon in args.horizons:
        selected = _horizon_frame(schedule, horizon)
        exceptions, readiness = _readiness(selected, Path(args.mgt), Path(args.aircraft_lines))
        results.append(export_horizon(schedule, horizon, Path(args.output_root) / horizon, exceptions, readiness))
    report = {"runs": results, "overall_status": "BLOCKED_NOT_OPTIMIZED" if any(r["status"] == "BLOCKED_NOT_OPTIMIZED" for r in results) else "READY_NOT_OPTIMIZED"}
    Path(args.output_root).mkdir(parents=True, exist_ok=True)
    (Path(args.output_root) / "horizon_run_summary.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
