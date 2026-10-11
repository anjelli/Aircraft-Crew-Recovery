#!/usr/bin/env python3
"""
run_pairing_experiment_166_days_clean.py

ONE REQUIRED EXPERIMENT
=======================
Planned schedule (configured source horizon; current data contains 155 days)
    -> deterministic 7% disruption
    -> validate existing pairings using supplied MGT.py + pairings_builder.py rules
    -> list exact violations
    -> run supplied ORIGINAL pairings_builder.py on disrupted active schedule
    -> compare pairing counts, flight coverage and runtime

This file is the ONLY new experiment code.
Do NOT modify originals/MGT.py or originals/pairings_builder.py.

Required source files
---------------------
originals/MGT.py
originals/pairings_builder.py
originals/MGT.xlsx
originals/fdp_rules.csv
originals/cost_parameters.csv
input/schedule.xlsx
input/pairing_report.xlsx

The disruption_log.csv is NOT required. This experiment creates its own reproducible
166-day disruption log so every changed flight can be shown and audited.

Outputs
-------
output/disruption_log_166_days.csv
output/disrupted_schedule_166_days.xlsx
output/pairing_experiment_166_days.xlsx

The final workbook contains:
    Summary
    Disruption Log
    Existing Pairing Violations
    New Pairings
    Disrupted Schedule

NOT included: MIP, column generation, aircraft-tail maintenance, 787 augmentation rules,
or any modification/reimplementation of the supplied pairing builder.
"""

from __future__ import annotations

import importlib.util
import logging
import math
import multiprocessing as mp
import platform
import sys
import time
from collections import Counter
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from mgt_effective import apply_mgt_processing_by_arrival_date
from project_paths import input_file, original_file

LOG = logging.getLogger("pairing_experiment")
ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"

# --------------------------------------------------------------------------------------
# Experiment definition
# --------------------------------------------------------------------------------------

import os as _os
# FIX (audit): source schedule has only 155 days; allow documented override via env HORIZON_DAYS (default 166 keeps original guard).
HORIZON_DAYS = int(_os.environ.get("HORIZON_DAYS", "166"))
SEED = 2026
DISRUPTION_RATE = 0.07
MIN_DISRUPTION_RATE = 0.05
MAX_DISRUPTION_RATE = 0.10

# Simulation assumption only; NOT an official Scoot rule.
DISRUPTION_MIX = (
    ("CANCELLED", 0.25),
    ("EQUIPMENT_CHANGED", 0.25),
    ("RETIMED", 0.50),
)
RETIME_MINUTES = (30, 60, 90)

# Required only because the supplied MGT.py expects aircraft-group input.
# This is a demo assumption, not an official airline master.
AIRCRAFT_GROUPS_DEMO = {
    "320": {"320", "32N", "32Q"},
    "787": set(),
}
MAX_LAYOVER_HOURS_DEMO = 72.0
MAX_LAYOVER_COMBINED_DEMO = 72.0
ASSUMPTION_TAG = "DEMO ASSUMPTION - NOT AN OFFICIAL SCOOT RULE"

# --------------------------------------------------------------------------------------
# Canonical source and prepared-input locations; no root or numbered-file fallback.
# --------------------------------------------------------------------------------------

def load_original_module(path: Path, module_name: str):
    """Load a supplied source file under the module name expected by multiprocessing."""
    parent = str(path.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)

    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load {path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------------------
# Data loading
# --------------------------------------------------------------------------------------


def load_schedule(path: Path) -> pd.DataFrame:
    df = pd.read_excel(path)
    required = [
        "Day", "Flt Num", "From", "To", "Dept Ctry", "Arvl Ctry", "Eqp",
        "STD", "STA", "STD_sin", "STA_sin",
    ]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"schedule.xlsx missing columns: {missing}")

    for c in ("Day", "STD", "STA", "STD_sin", "STA_sin"):
        df[c] = pd.to_datetime(df[c], errors="coerce")
        if df[c].isna().any():
            raise ValueError(f"schedule.xlsx has invalid/missing values in {c}")

    df["Day"] = df["Day"].dt.normalize()
    df["Eqp"] = df["Eqp"].astype(str).str.strip()
    df["flight_id"] = (
        df["Flt Num"].astype(int).astype(str)
        + "_"
        + df["STD"].dt.strftime("%Y%m%dT%H%M%S")
    )

    if df["flight_id"].duplicated().any():
        raise ValueError("schedule.xlsx contains duplicate flight_id values")

    return df.sort_values(["STD_sin", "flight_id"]).reset_index(drop=True)


def load_pairing_report(path: Path) -> pd.DataFrame:
    p = pd.read_excel(path)
    required = ["Pairing ID", "Flight Date", "Flight Num", "STD_local", "Duty Start"]
    missing = [c for c in required if c not in p.columns]
    if missing:
        raise ValueError(f"pairing_report.xlsx missing columns: {missing}")

    # FIX (audit): do NOT forward-fill blank Pairing IDs; they are unpaired leftover rows
    # and ffill silently glued them onto the preceding real pairing (8473).
    _unp = p["Pairing ID"].isna()
    if _unp.any():
        Path("output/audit").mkdir(parents=True, exist_ok=True)
        p[_unp].to_csv("output/audit/unpaired_rows_excluded_from_pairing_report.csv", index=False)
        LOG.warning("%d pairing-report rows have blank Pairing ID; excluded from pairing validation (see output/audit)", int(_unp.sum()))
        p = p[~_unp].copy()
    p["Flight Date"] = pd.to_datetime(p["Flight Date"], errors="coerce").dt.normalize()
    if p["Flight Date"].isna().any():
        raise ValueError("pairing_report.xlsx contains invalid Flight Date values")
    return p


def load_fdp(path: Path) -> dict:
    df = pd.read_csv(path, encoding="utf-8-sig")
    required = {"time_window", *(f"limit{i}" for i in range(1, 9))}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"fdp_rules.csv missing columns: {sorted(missing)}")
    return {
        str(r["time_window"]).strip(): [float(r[f"limit{i}"]) for i in range(1, 9)]
        for _, r in df.iterrows()
    }


def load_costs(path: Path) -> dict:
    df = pd.read_csv(path, encoding="utf-8-sig")
    if not {"parameter", "value"}.issubset(df.columns):
        raise ValueError("cost_parameters.csv must contain parameter and value columns")
    return dict(zip(df["parameter"].astype(str).str.strip(), df["value"]))


def derive_airport_to_country(schedule: pd.DataFrame) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for airport, g in schedule.groupby("To"):
        vals = {str(v).strip().upper() for v in g["Arvl Ctry"].dropna() if str(v).strip()}
        if len(vals) == 1:
            mapping[str(airport).strip().upper()] = next(iter(vals))
    for airport, g in schedule.groupby("From"):
        vals = {str(v).strip().upper() for v in g["Dept Ctry"].dropna() if str(v).strip()}
        if len(vals) == 1:
            mapping.setdefault(str(airport).strip().upper(), next(iter(vals)))
    return mapping


def report_key(row) -> str:
    return (
        str(int(row["Flight Num"]))
        + "_"
        + pd.Timestamp(row["Flight Date"]).strftime("%Y%m%dT")
        + pd.Timestamp(row["STD_local"]).strftime("%H%M%S")
    )


def group_report(report: pd.DataFrame):
    groups = []
    for pid, g in report.groupby("Pairing ID", sort=False):
        g = g.reset_index(drop=True).copy()
        g["_key"] = [report_key(r) for _, r in g.iterrows()]
        g["_duty_start"] = g["Duty Start"].notna()
        groups.append((str(int(pid)), g))
    return groups


def derive_layover_stations(report: pd.DataFrame, hub: str) -> list[str]:
    stations = set()
    for _, g in report.groupby("Pairing ID", sort=False):
        g = g.reset_index(drop=True)
        for i in range(1, len(g)):
            if (
                pd.notna(g.loc[i, "Duty Start"])
                and str(g.loc[i - 1, "To"]).strip().upper() == str(g.loc[i, "From"]).strip().upper()
                and str(g.loc[i, "From"]).strip().upper() != hub
            ):
                stations.add(str(g.loc[i, "From"]).strip().upper())
    return sorted(stations)


# --------------------------------------------------------------------------------------
# External deterministic disruption
# --------------------------------------------------------------------------------------


def simulate_disruption(base: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Apply a reproducible 7% disruption and return (full schedule, disruption log)."""
    d = base.copy().reset_index(drop=True)
    for c in ("STD", "STA", "STD_sin", "STA_sin", "Eqp"):
        d[f"original_{c}"] = d[c]

    d["active"] = True
    d["disruption_status"] = "UNCHANGED"
    d["disruption_type"] = "NONE"
    d["retime_minutes"] = 0
    d["equipment_changed"] = False

    n = int(round(len(d) * DISRUPTION_RATE))
    rng = np.random.default_rng(SEED)
    eligible_indices = d.sort_values("flight_id").index.to_numpy()
    chosen = rng.permutation(eligible_indices)[:n]

    n_cancel = int(round(n * DISRUPTION_MIX[0][1]))
    n_eqp = int(round(n * DISRUPTION_MIX[1][1]))

    eqp_values = sorted(d["Eqp"].dropna().astype(str).str.strip().unique())
    if len(eqp_values) < 2:
        n_eqp = 0
    n_retime = n - n_cancel - n_eqp

    for k, idx in enumerate(chosen):
        if k < n_cancel:
            d.at[idx, "active"] = False
            d.at[idx, "disruption_status"] = "CANCELLED"
            d.at[idx, "disruption_type"] = "CANCELLATION"
        elif k < n_cancel + n_eqp:
            old = str(d.at[idx, "Eqp"]).strip()
            alternatives = [x for x in eqp_values if x != old]
            new = str(rng.choice(alternatives))
            d.at[idx, "Eqp"] = new
            d.at[idx, "equipment_changed"] = True
            d.at[idx, "disruption_status"] = "EQUIPMENT_CHANGED"
            d.at[idx, "disruption_type"] = f"EQP_{old}>{new}"
        else:
            mins = int(rng.choice(RETIME_MINUTES))
            delta = pd.Timedelta(minutes=mins)
            for c in ("STD", "STA", "STD_sin", "STA_sin"):
                d.at[idx, c] = pd.Timestamp(d.at[idx, c]) + delta
            d.at[idx, "retime_minutes"] = mins
            d.at[idx, "disruption_status"] = "RETIMED"
            d.at[idx, "disruption_type"] = f"RETIME_+{mins}MIN"

    d = d.sort_values(["STD_sin", "flight_id"]).reset_index(drop=True)
    disrupted = d[d["disruption_status"] != "UNCHANGED"].copy()

    log_cols = [
        "flight_id", "Flt Num", "Day", "From", "To",
        "original_STD", "STD", "original_STA", "STA",
        "original_Eqp", "Eqp", "disruption_status", "disruption_type",
        "retime_minutes", "equipment_changed", "active",
    ]
    return d, disrupted[log_cols].copy()


# --------------------------------------------------------------------------------------
# MGT through the supplied MGT.py
# --------------------------------------------------------------------------------------


def attach_mgt(df: pd.DataFrame, mgt_module, mgt_xlsx: Path,
                airport_to_country: dict[str, str], subtypes: list[str]) -> pd.DataFrame:
    mgt_table = pd.read_excel(mgt_xlsx)
    processed, _, _ = apply_mgt_processing_by_arrival_date(
        df.copy().reset_index(drop=True),
        mgt_table,
        mgt_module,
        AIRCRAFT_GROUPS_DEMO,
        airport_to_country,
        subtypes,
        arrival_date_col="STA",
        sta_col="STA",
    )
    missing = processed["MGT"].isna()
    if missing.any():
        cols = [c for c in ("Day", "Flt Num", "To", "Eqp", "STA_local") if c in processed.columns]
        bad = processed.loc[missing, cols].drop_duplicates().head(20)
        raise RuntimeError(
            "No applicable MGT rule matched these flight arrivals after Start Date/End Date/Freq "
            "filtering. Expired rules are not reused; reconcile the source MGT effective dates "
            + "\n" + bad.to_string(index=False)
        )
    return processed


# --------------------------------------------------------------------------------------
# Exact validation wrapper around supplied builder rules
# --------------------------------------------------------------------------------------


class RuleChecker:
    def __init__(self, pb, fdp, duty_start_buffer, fdp_end_buffer, duty_end_buffer,
                 max_layover_hours, layover_stations):
        self.pb = pb
        self.fdp = fdp
        self.dsb = duty_start_buffer
        self.feb = fdp_end_buffer
        self.deb = duty_end_buffer
        self.max_layover = timedelta(hours=max_layover_hours)
        self.stations = set(layover_stations)
        self.hub = pb.HUB

    def check_duty(self, duty: list[dict]) -> list[dict]:
        if not duty:
            return []
        pb = self.pb
        out = []
        ids = [f["flight_id"] for f in duty]

        if len(ids) != len(set(ids)):
            out.append({
                "flight_id": ids[-1],
                "type": "DUPLICATE_FLIGHT",
                "rule": "Each flight appears once per duty",
                "value": len(ids), "limit": 1, "unit": "count",
                "explanation": "Duplicate flight ID inside one duty.",
            })
            return out

        if len(duty) > pb.MAX_SECTORS_PER_DUTY:
            out.append({
                "flight_id": ids[-1], "type": "SECTOR_LIMIT",
                "rule": "pairings_builder.MAX_SECTORS_PER_DUTY",
                "value": len(duty), "limit": pb.MAX_SECTORS_PER_DUTY, "unit": "sectors",
                "explanation": "Duty exceeds the supplied builder sector cap.",
            })

        duty_start = duty[0]["STD"] - timedelta(minutes=self.dsb)
        duty_start_local = duty[0]["STD_local"] - timedelta(minutes=self.dsb)
        duty_end = duty[-1]["STA"] + timedelta(
            minutes=self.deb if len(duty) >= pb.MAX_SECTORS_PER_DUTY else self.feb
        )
        total_hours = (duty_end - duty_start).total_seconds() / 3600.0
        band = pb.get_time_band_key(duty_start_local)
        limits = self.fdp.get(band)
        if limits is None:
            out.append({
                "flight_id": ids[-1], "type": "MISSING_FDP_BAND",
                "rule": "fdp_rules.csv time band", "value": band,
                "limit": "configured", "unit": "",
                "explanation": f"No FDP limits found for duty start band {band}.",
            })
        else:
            limit = limits[min(len(duty) - 1, 7)]
            if total_hours > limit + 1e-9:
                out.append({
                    "flight_id": ids[-1], "type": "FDP_LIMIT",
                    "rule": f"FDP limit ({band})", "value": round(total_hours, 2),
                    "limit": limit, "unit": "hours",
                    "explanation": f"Duty is {total_hours:.2f}h against {limit:.2f}h allowed.",
                })

        for k in range(1, len(duty)):
            prev, cur = duty[k - 1], duty[k]
            if prev["To"] != cur["From"]:
                out.append({
                    "flight_id": cur["flight_id"], "type": "CONNECTIVITY",
                    "rule": "Previous arrival station = next departure station",
                    "value": f"{prev['To']} -> {cur['From']}", "limit": "same station", "unit": "",
                    "explanation": "Successive sectors do not connect at the same airport.",
                })
                continue

            actual = cur["STD"] - prev["STA"]
            required = pb._required_mgt(k, prev["MGT_td"], prev["Eqp"], cur["Eqp"])
            if actual < required:
                out.append({
                    "flight_id": cur["flight_id"], "type": "MGT_CONNECTION",
                    "rule": "pairings_builder._required_mgt",
                    "value": round(actual.total_seconds() / 60, 1),
                    "limit": round(required.total_seconds() / 60, 1), "unit": "minutes",
                    "explanation": f"Connection has {actual.total_seconds()/60:.1f} min; requires "
                                   f"{required.total_seconds()/60:.1f} min.",
                })

        if duty[0]["From"] == self.hub and duty[-1]["To"] == self.hub:
            start_eqp = duty[0]["Eqp"]
            for k in range(1, len(duty)):
                if not pb.eqp_allowed_single_day(k, start_eqp, duty[k - 1]["Eqp"], duty[k]["Eqp"]):
                    out.append({
                        "flight_id": duty[k]["flight_id"], "type": "EQUIPMENT_CONTINUITY",
                        "rule": "pairings_builder.eqp_allowed_single_day",
                        "value": f"{duty[k-1]['Eqp']} -> {duty[k]['Eqp']}",
                        "limit": f"start equipment {start_eqp}", "unit": "",
                        "explanation": "Equipment progression violates the supplied hub-turn rule.",
                    })
        return out

    def check_pairing(self, seq: list[dict], duty_splits: list[list[int]]) -> list[dict]:
        out = []
        for f in seq:
            if not bool(f.get("active", True)):
                out.append({
                    "flight_id": f["flight_id"], "type": "CANCELLED_FLIGHT",
                    "rule": "Flight must be active", "value": "CANCELLED", "limit": "ACTIVE", "unit": "",
                    "explanation": "This flight was cancelled by the disruption simulation.",
                })
        if out:
            return out

        duties = [[seq[i] for i in idxs] for idxs in duty_splits]
        for duty in duties:
            out.extend(self.check_duty(duty))

        for i in range(len(duties) - 1):
            a, b = duties[i], duties[i + 1]
            if a[-1]["To"] != b[0]["From"]:
                out.append({
                    "flight_id": b[0]["flight_id"], "type": "DUTY_BOUNDARY_STATION",
                    "rule": "Duty boundary station continuity",
                    "value": f"{a[-1]['To']} -> {b[0]['From']}", "limit": "same station", "unit": "",
                    "explanation": "Consecutive duties do not meet at the same station.",
                })
                continue

            da = self.pb._build_duty_record(a, self.stations, self.dsb, self.feb, self.deb)
            db = self.pb._build_duty_record(b, self.stations, self.dsb, self.feb, self.deb)
            actual = db["start_time"] - da["end_time"]

            if actual <= timedelta(0):
                out.append({
                    "flight_id": b[0]["flight_id"], "type": "NON_POSITIVE_REST",
                    "rule": "Inter-duty rest > 0", "value": round(actual.total_seconds() / 3600, 2),
                    "limit": 0, "unit": "hours",
                    "explanation": "Next duty starts before or at previous duty end.",
                })
                continue

            if actual > self.max_layover:
                out.append({
                    "flight_id": b[0]["flight_id"], "type": "MAX_LAYOVER",
                    "rule": f"Maximum layover ({ASSUMPTION_TAG})",
                    "value": round(actual.total_seconds() / 3600, 2),
                    "limit": self.max_layover.total_seconds() / 3600, "unit": "hours",
                    "explanation": "Inter-duty rest exceeds the assumed layover cap.",
                })
                continue

            if not self.pb.valid_rest(da, db, self.max_layover):
                out.append({
                    "flight_id": b[0]["flight_id"], "type": "REST_MINIMUM",
                    "rule": "pairings_builder.valid_rest/_required_rest",
                    "value": round(actual.total_seconds() / 3600, 2),
                    "limit": "builder rest requirement", "unit": "hours",
                    "explanation": "Inter-duty rest fails the supplied builder rest rule.",
                })
        return out


# --------------------------------------------------------------------------------------
# Existing pairing validation
# --------------------------------------------------------------------------------------


_VKEY = {"flight_id": "Flight ID", "type": "Violation Type", "rule": "Rule", "value": "Value",
         "limit": "Limit", "unit": "Unit", "explanation": "Explanation"}


def _norm_violation(v: dict) -> dict:
    """FIX (audit): RuleChecker emits lowercase keys; the sheet expects Title-case columns."""
    return {_VKEY.get(k, k): val for k, val in v.items()}


def validate_existing_pairings(report_groups, record_lookup, checker: RuleChecker,
                               start: pd.Timestamp, end: pd.Timestamp):
    rows = []
    violations = []
    skipped_boundary = 0

    for pid, g in report_groups:
        inside = g["Flight Date"].between(start, end)
        if not inside.any():
            continue
        if not inside.all():
            skipped_boundary += 1
            continue

        seq = []
        missing = []
        for _, r in g.iterrows():
            f = record_lookup.get(r["_key"])
            if f is None:
                missing.append(r["_key"])
            else:
                f = dict(f)
                f["_duty_marker"] = bool(r["_duty_start"])
                seq.append(f)

        if missing:
            for fid in missing:
                violations.append({
                    "Pairing ID": pid,
                    "Flight ID": fid,
                    "Violation Type": "MISSING_SCHEDULE_MATCH",
                    "Rule": "Pairing report flight must exist in schedule",
                    "Value": "not found", "Limit": "present", "Unit": "",
                    "Explanation": "Could not match the pairing-report flight to the source schedule.",
                })
            rows.append({
                "Pairing ID": pid, "Status": "INVALID",
                "Number of Violations": len(missing),
                "Flight IDs": "|".join(missing), "Disruption Impact": "UNKNOWN",
            })
            continue

        duties_idx = []
        current = []
        for i, f in enumerate(seq):
            if f["_duty_marker"] and current:
                duties_idx.append(current)
                current = []
            current.append(i)
        if current:
            duties_idx.append(current)

        viol = checker.check_pairing(seq, duties_idx)
        impacts = sorted({
            "CANCELLED" if not f.get("active", True) else f.get("disruption_status", "UNCHANGED")
            for f in seq
            if (not f.get("active", True)) or f.get("disruption_status") != "UNCHANGED"
        })

        for v in viol:
            violations.append({
                "Pairing ID": pid,
                "Flight ID": v["flight_id"],
                "Violation Type": v["type"],
                "Rule": v["rule"],
                "Value": v["value"],
                "Limit": v["limit"],
                "Unit": v["unit"],
                "Explanation": v["explanation"],
                "Disruption Involved": "YES" if impacts else "NO",
            })

        rows.append({
            "Pairing ID": pid,
            "Status": "INVALID" if viol else "VALID",
            "Number of Violations": len(viol),
            "Flight IDs": "|".join(f["flight_id"] for f in seq),
            "Disruption Impact": "|".join(impacts) if impacts else "NONE",
        })

    return pd.DataFrame(rows), violations, skipped_boundary


# --------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------


def autosize(ws, max_width=55):
    from openpyxl.utils import get_column_letter
    for i, col in enumerate(ws.iter_cols(min_row=1, max_row=min(ws.max_row, 300)), start=1):
        width = 10
        for cell in col:
            if cell.value is not None:
                width = max(width, min(max_width, len(str(cell.value)) + 2))
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"
    if ws.max_row and ws.max_column:
        ws.auto_filter.ref = ws.dimensions


def write_workbook(path: Path, summary: dict, disruption_log: pd.DataFrame,
                   violations: list[dict], generated_rows: list[dict], disrupted_schedule: pd.DataFrame):
    from openpyxl.styles import Font

    path.parent.mkdir(parents=True, exist_ok=True)

    violation_cols = [
        "Pairing ID", "Flight ID", "Violation Type", "Rule", "Value", "Limit", "Unit",
        "Explanation", "Disruption Involved",
    ]
    generated_cols = [
        "Horizon", "New Pairing ID", "Pairing Type", "Flight IDs", "Number of Sectors",
        "Number of Duties (builder metadata)", "Start", "End", "Duration Hours",
        "Layover Station", "Layover Hours", "Equipment Changes", "Touches Disrupted Flight",
    ]
    schedule_cols = [
        "flight_id", "Day", "Flt Num", "From", "To", "STD", "STA", "STD_sin", "STA_sin", "Eqp",
        "active", "disruption_status", "disruption_type", "original_STD", "original_STA", "original_Eqp",
        "retime_minutes", "equipment_changed",
    ]

    viol_df = pd.DataFrame(violations, columns=violation_cols)
    gen_df = pd.DataFrame(generated_rows, columns=generated_cols)
    schedule_df = disrupted_schedule[[c for c in schedule_cols if c in disrupted_schedule.columns]].copy()

    with pd.ExcelWriter(
        path,
        engine="openpyxl",
        datetime_format="yyyy-mm-dd hh:mm",
        date_format="yyyy-mm-dd",
    ) as writer:
        pd.DataFrame([summary]).to_excel(writer, sheet_name="Summary", index=False)
        disruption_log.to_excel(writer, sheet_name="Disruption Log", index=False)
        viol_df.to_excel(writer, sheet_name="Existing Pairing Violations", index=False)
        gen_df.to_excel(writer, sheet_name="New Pairings", index=False)
        schedule_df.to_excel(writer, sheet_name="Disrupted Schedule", index=False)

        for ws in writer.book.worksheets:
            for cell in ws[1]:
                cell.font = Font(bold=True)
            autosize(ws)


def write_disrupted_schedule_file(path: Path, disrupted_schedule: pd.DataFrame):
    from openpyxl.styles import Font

    cols = [
        "flight_id", "Day", "Flt Num", "From", "To", "STD", "STA", "STD_sin", "STA_sin", "Eqp",
        "active", "disruption_status", "disruption_type", "original_STD", "original_STA", "original_Eqp",
        "retime_minutes", "equipment_changed",
    ]
    with pd.ExcelWriter(path, engine="openpyxl", datetime_format="yyyy-mm-dd hh:mm", date_format="yyyy-mm-dd") as writer:
        disrupted_schedule[cols].to_excel(writer, sheet_name="Disrupted Schedule", index=False)
        ws = writer.book["Disrupted Schedule"]
        for cell in ws[1]:
            cell.font = Font(bold=True)
        autosize(ws)


# --------------------------------------------------------------------------------------
# Main experiment
# --------------------------------------------------------------------------------------


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    mp.freeze_support()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    t0 = time.time()

    # Only these seven source files are required.
    mgt_path = original_file("MGT.py")
    builder_path = original_file("pairings_builder.py")
    schedule_path = input_file("schedule.xlsx")
    report_path = input_file("pairing_report.xlsx")
    mgt_xlsx_path = original_file("MGT.xlsx")
    fdp_path = original_file("fdp_rules.csv")
    cost_path = original_file("cost_parameters.csv")

    for label, p in [
        ("MGT.py", mgt_path),
        ("pairings_builder.py", builder_path),
        ("schedule.xlsx", schedule_path),
        ("pairing_report.xlsx", report_path),
        ("MGT.xlsx", mgt_xlsx_path),
        ("fdp_rules.csv", fdp_path),
        ("cost_parameters.csv", cost_path),
    ]:
        LOG.info("%s: %s", label, p)

    if builder_path.name.lower() != "pairings_builder.py":
        raise RuntimeError(
            f"For Windows multiprocessing, the original builder file must be named pairings_builder.py; got {builder_path.name}"
        )

    # Load under the actual import names used by worker processes.
    mgt = load_original_module(mgt_path, "MGT")
    pb = load_original_module(builder_path, "pairings_builder")
    LOG.info("Builder execution mode: original multiprocessing Pool")

    for fn in ("apply_mgt_processing", "resolve_mgt_minutes"):
        if not hasattr(mgt, fn):
            raise RuntimeError(f"Supplied MGT.py is missing {fn}()")
    for fn in (
        "build_all_pairings", "_build_duty_record", "_required_mgt", "valid_rest",
        "valid_sin_connection", "eqp_allowed_single_day", "get_time_band_key",
    ):
        if not hasattr(pb, fn):
            raise RuntimeError(f"Supplied pairings_builder.py is missing {fn}()")

    schedule = load_schedule(schedule_path)
    report = load_pairing_report(report_path)
    fdp = load_fdp(fdp_path)
    costs = load_costs(cost_path)

    dsb = int(float(costs["duty_start_buffer"]))
    feb = int(float(costs["fdp_end_buffer"]))
    deb = int(float(costs["reported_duty_end_buffer"]))

    source_start = schedule["Day"].min()
    source_end = schedule["Day"].max()
    requested_end = source_start + pd.Timedelta(days=HORIZON_DAYS - 1)

    LOG.info(
        "Source schedule: %s to %s | %d flights | %d calendar days",
        source_start.date(), source_end.date(), len(schedule), schedule["Day"].nunique()
    )

    if requested_end > source_end:
        raise RuntimeError(
            f"Source schedule does not contain the requested {HORIZON_DAYS}-day horizon. "
            f"Required through {requested_end.date()}, available through {source_end.date()}."
        )

    horizon = schedule[schedule["Day"].between(source_start, requested_end)].copy()
    present_days = horizon["Day"].nunique()
    if present_days != HORIZON_DAYS:
        raise RuntimeError(
            f"Requested {HORIZON_DAYS} calendar days but only {present_days} days contain schedule data."
        )

    LOG.info(
        "[166D] Running full source horizon: %s to %s (%d flights)",
        source_start.date(), requested_end.date(), len(horizon)
    )

    # Demo parameters used by the supplied source modules.
    layover_stations = derive_layover_stations(report, pb.HUB)
    airport_to_country = derive_airport_to_country(schedule)
    subtypes = sorted(schedule["Eqp"].astype(str).str.upper().unique())

    LOG.info("Demo layover stations reconstructed from pairing report: %s", layover_stations)
    LOG.info(
        "Demo assumptions: aircraft_groups=%s | max_layover=%.1fh | max_combined_layover=%.1fh",
        AIRCRAFT_GROUPS_DEMO, MAX_LAYOVER_HOURS_DEMO, MAX_LAYOVER_COMBINED_DEMO
    )

    # 1. Disruption
    disrupted, disruption_log = simulate_disruption(horizon)
    n_disrupted = len(disruption_log)
    disruption_pct = 100.0 * n_disrupted / len(horizon)
    counts = Counter(disruption_log["disruption_status"])

    if not (MIN_DISRUPTION_RATE * 100 <= disruption_pct <= MAX_DISRUPTION_RATE * 100):
        raise RuntimeError(f"Disruption rate {disruption_pct:.2f}% is outside requested 5-10%.")

    disruption_csv = OUTPUT_DIR / "disruption_log_166_days.csv"
    disruption_xlsx = OUTPUT_DIR / "disrupted_schedule_166_days.xlsx"
    result_xlsx = OUTPUT_DIR / "pairing_experiment_166_days.xlsx"
    disruption_log.to_csv(disruption_csv, index=False)
    write_disrupted_schedule_file(disruption_xlsx, disrupted)

    LOG.info(
        "[166D] Disruption: %d/%d = %.2f%% | %s",
        n_disrupted, len(horizon), disruption_pct, dict(counts)
    )
    LOG.info("[166D] Disruption log: %s", disruption_csv)
    LOG.info("[166D] Disrupted schedule: %s", disruption_xlsx)

    # 2. MGT using supplied MGT.py
    mgt_start = time.time()
    processed = attach_mgt(disrupted, mgt, mgt_xlsx_path, airport_to_country, subtypes)
    LOG.info("[166D] MGT processing: %.2fs", time.time() - mgt_start)

    record_lookup = {r["flight_id"]: r for r in processed.to_dict("records")}
    active = processed[processed["active"]].copy().sort_values(["STD", "flight_id"]).reset_index(drop=True)
    active_ids = set(active["flight_id"])

    # 3. Existing pairing validation
    report_groups = group_report(report)
    checker = RuleChecker(
        pb, fdp, dsb, feb, deb,
        MAX_LAYOVER_HOURS_DEMO, layover_stations,
    )

    validate_start = time.time()
    existing_df, violations, skipped_boundary = validate_existing_pairings(
        report_groups,
        record_lookup,
        checker,
        source_start,
        requested_end,
    )
    LOG.info(
        "[166D] Existing pairing validation: %.2fs | checked=%d valid=%d invalid=%d boundary_skipped=%d",
        time.time() - validate_start,
        len(existing_df),
        int((existing_df["Status"] == "VALID").sum()) if len(existing_df) else 0,
        int((existing_df["Status"] == "INVALID").sum()) if len(existing_df) else 0,
        skipped_boundary,
    )

    existing_valid_cover = set()
    if len(existing_df):
        for _, r in existing_df[existing_df["Status"] == "VALID"].iterrows():
            existing_valid_cover.update(str(r["Flight IDs"]).split("|"))
    existing_valid_cover &= active_ids

    # 4. Original pairing builder on the disrupted active schedule
    layover_df = pd.DataFrame({"Station": layover_stations})
    builder_start = time.time()
    pairs, metadata = pb.build_all_pairings(
        active,
        fdp,
        dsb,
        feb,
        deb,
        MAX_LAYOVER_HOURS_DEMO,
        layover_df,
        MAX_LAYOVER_COMBINED_DEMO,
        source_start.date(),
        requested_end.date(),
    )
    generation_runtime = time.time() - builder_start

    family_counts = Counter(str(m.get("pairing_type", "UNKNOWN")) for m in metadata)
    generated_cover = {f["flight_id"] for p in pairs for f in p} & active_ids

    # Give every generated candidate a stable experiment ID and useful metadata.
    generated_rows = []
    touched_count = 0
    sequence_seen = set()
    duplicate_sequences = 0
    disruption_status = {
        r["flight_id"]: ("CANCELLED" if not r["active"] else r["disruption_status"])
        for r in processed.to_dict("records")
    }

    for i, (seq, meta) in enumerate(zip(pairs, metadata), start=1):
        ids = [f["flight_id"] for f in seq]
        seq_key = tuple(ids)
        if seq_key in sequence_seen:
            duplicate_sequences += 1
        sequence_seen.add(seq_key)

        touched = sorted({
            disruption_status[fid]
            for fid in ids
            if disruption_status.get(fid, "UNCHANGED") != "UNCHANGED"
        })
        if touched:
            touched_count += 1

        generated_rows.append({
            "Horizon": "166 days",
            "New Pairing ID": f"NEW_166D_{i:07d}",
            "Pairing Type": str(meta.get("pairing_type", "UNKNOWN")),
            "Flight IDs": "|".join(ids),
            "Number of Sectors": int(meta.get("num_sectors", len(seq))),
            "Number of Duties (builder metadata)": meta.get("num_duties"),
            "Start": seq[0]["STD"],
            "End": seq[-1]["STA"],
            "Duration Hours": round((seq[-1]["STA"] - seq[0]["STD"]).total_seconds() / 3600, 2),
            "Layover Station": meta.get("layover_station") or "",
            "Layover Hours": (
                round(float(meta["layover_hours"]), 2)
                if meta.get("layover_hours") is not None else None
            ),
            "Equipment Changes": sum(a["Eqp"] != b["Eqp"] for a, b in zip(seq, seq[1:])),
            "Touches Disrupted Flight": "|".join(touched) if touched else "NO",
        })

    combined_cover = existing_valid_cover | generated_cover
    uncovered = active_ids - combined_cover

    violation_counts = Counter(v["Violation Type"] for v in violations)
    n_existing_valid = int((existing_df["Status"] == "VALID").sum()) if len(existing_df) else 0
    n_existing_invalid = int((existing_df["Status"] == "INVALID").sum()) if len(existing_df) else 0

    summary = {
        "Horizon": "166 days",
        "Start Date": source_start.date(),
        "End Date": requested_end.date(),
        "Source Flights": len(horizon),
        "Disrupted Flights": n_disrupted,
        "Disruption %": round(disruption_pct, 2),
        "Cancelled": counts.get("CANCELLED", 0),
        "Retimed": counts.get("RETIMED", 0),
        "Equipment Changed": counts.get("EQUIPMENT_CHANGED", 0),
        "Active Flights After Disruption": len(active),
        "Existing Pairings Checked": len(existing_df),
        "Existing Pairings Valid": n_existing_valid,
        "Existing Pairings Invalid": n_existing_invalid,
        "Total Violations": len(violations),
        "Single-Day Pairings Generated": family_counts.get("single_day", 0),
        "Multi-Day Pairings Generated": family_counts.get("multi_day", 0),
        "Combined Post-Layover Generated": family_counts.get("combined_post_layover", 0),
        "Combined Pre-Layover Generated": family_counts.get("combined_pre_layover", 0),
        "Total New Pairings Generated": len(generated_rows),
        "Flights Covered by New Pairings": len(generated_cover),
        "Flights Covered by Existing Valid": len(existing_valid_cover),
        "Flights Covered by Existing Valid + New": len(combined_cover),
        "Flights Uncovered": len(uncovered),
        "New Pairings Touching Disrupted Flights": touched_count,
        "Duplicate Generated Sequences": duplicate_sequences,
        "Pairing Generation Runtime (s)": round(generation_runtime, 2),
        "Total Runtime (s)": round(time.time() - t0, 2),
        "Status": "OK",
        "Assumptions": (
            f"{ASSUMPTION_TAG}; aircraft_groups={AIRCRAFT_GROUPS_DEMO}; "
            f"layover stations reconstructed from pairing_report={layover_stations}; "
            f"max layover={MAX_LAYOVER_HOURS_DEMO}h; "
            f"max combined layover={MAX_LAYOVER_COMBINED_DEMO}h; "
            f"disruption seed={SEED}, target rate={DISRUPTION_RATE:.0%}, "
            f"retime choices={RETIME_MINUTES}."
        ),
        "Notes": (
            f"{skipped_boundary} existing pairings crossed the 166-day horizon boundary and were not evaluated. "
            "New pairings are candidate columns; this experiment does not perform the later MIP/crew assignment stage."
        ),
    }

    write_workbook(
        result_xlsx,
        summary,
        disruption_log,
        violations,
        generated_rows,
        disrupted,
    )

    # 5. Terminal report
    coverage_pct = 100.0 * len(combined_cover) / len(active) if len(active) else 0.0
    LOG.info("[166D] Existing violations by type: %s", dict(sorted(violation_counts.items())))
    LOG.info(
        "[166D] New pairings: %d | existing-valid + new coverage: %d/%d (%.2f%%) | uncovered=%d",
        len(generated_rows), len(combined_cover), len(active), coverage_pct, len(uncovered)
    )
    LOG.info("[166D] Pairing generation runtime: %.2fs", generation_runtime)
    LOG.info("[166D] Results workbook: %s", result_xlsx)

    print("\n" + "=" * 116)
    print("166-DAY CREW PAIRING EXPERIMENT")
    print("-" * 116)
    print(f"Source:                 {source_start.date()} -> {requested_end.date()} | {len(horizon):,} flights")
    print(f"Disruption:             {n_disrupted:,} ({disruption_pct:.2f}%) | {dict(counts)}")
    print(f"Existing pairings:      {len(existing_df):,} checked | {n_existing_valid:,} valid | {n_existing_invalid:,} invalid")
    print(f"Exact violations:       {len(violations):,}")
    print(f"New pairings:           {len(generated_rows):,}")
    print(f"Combined candidate:     {len(combined_cover):,}/{len(active):,} active flights covered | {len(uncovered):,} uncovered")
    print(f"Builder runtime:        {generation_runtime:.2f}s")
    print(f"Results workbook:       {result_xlsx}")
    print(f"Disruption log:         {disruption_csv}")
    print(f"Disrupted schedule:     {disruption_xlsx}")
    print("=" * 116)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
