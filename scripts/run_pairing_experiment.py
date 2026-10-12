#!/usr/bin/env python3
"""
run_pairing_experiment_1_week_1_month.py

TEAM EXPERIMENT
----------------
PLANNED SCHEDULE
    -> 1 week / 1 month
    -> 5-10% deterministic seeded disruption
    -> validate EXISTING crew pairings with the ORIGINAL builder/MGT rules
    -> list exact violations
    -> run the ORIGINAL pairings_builder on the disrupted schedule
    -> compare counts, coverage and runtime

IMPORTANT
---------
* MGT.py and pairings_builder.py are loaded from disk and NEVER edited.
* No MIP / column generation / aircraft-tail maintenance is implemented here.
* No schedule data is fabricated or extrapolated.

OUTPUT: output/
    pairing_experiment_1_week.xlsx
    pairing_experiment_1_month.xlsx

Each workbook has exactly four sheets:
    Summary | Existing Pairing Violations | New Pairings | Disrupted Schedule
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import math
import multiprocessing as mp
import platform
import subprocess
import sys
import time
from collections import Counter
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
for _project_path in (PROJECT_ROOT, SRC_DIR):
    if str(_project_path) not in sys.path:
        sys.path.insert(0, str(_project_path))

from crew_recovery.mgt_effective import apply_mgt_processing_by_arrival_date
from crew_recovery.project_paths import input_file, original_file
from crew_recovery.pairing_columns import build_pairing_columns, independent_pairing_validator

LOG = logging.getLogger("pairing_experiment")

ROOT = PROJECT_ROOT
OUTPUT_DIR = ROOT / "output"

SEED = 2026
DISRUPTION_RATE = 0.07
MIN_RATE = 0.05
MAX_RATE = 0.10

# External demo assumptions only. These are NOT official Scoot rules.
ASSUMPTION_TAG = "DEMO ASSUMPTION - NOT AN OFFICIAL SCOOT RULE"
AIRCRAFT_GROUPS_DEMO = {
    "320": {"320", "32N", "32Q"},
    "787": set(),
}
MAX_LAYOVER_HOURS_DEMO = 72.0
MAX_LAYOVER_COMBINED_DEMO = 72.0
DEFAULT_RETIME_MINUTES = [30, 60, 90]

HORIZONS = [
    ("1 week", "1W", 7),
    ("1 month", "1M", 1),
]

EXPECTED_SHEETS = [
    "Summary",
    "Existing Pairing Violations",
    "New Pairings",
    "Disrupted Schedule",
]


# --------------------------------------------------------------------------------------
# Canonical source and prepared-input locations; no root or numbered-file fallback.
# --------------------------------------------------------------------------------------

def load_module(path: Path, public_name: str):
    """Load a supplied Python source file without changing it."""
    parent = str(path.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    spec = importlib.util.spec_from_file_location(public_name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[public_name] = mod
    spec.loader.exec_module(mod)
    return mod


class _SerialPool:
    """Drop-in Pool for dynamically loaded non-standard filenames on spawn platforms."""
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def map(self, fn, items):
        return [fn(x) for x in items]


def prepare_builder(pb, pb_path: Path):
    """Preserve builder logic; only fall back to serial Pool when worker import is unsafe."""
    start_method = mp.get_start_method(allow_none=True)
    exact_importable = pb_path.name.lower() == "pairings_builder.py"
    spawn_like = start_method in ("spawn", "forkserver") or platform.system() == "Windows"
    if spawn_like and not exact_importable:
        pb.Pool = _SerialPool
        return "serial Pool (original builder logic unchanged; non-standard filename is not spawn-importable)"
    return f"original multiprocessing Pool; start_method={start_method or 'default'}"


# --------------------------------------------------------------------------------------
# Data loading / preparation
# --------------------------------------------------------------------------------------

def load_schedule(path: Path) -> pd.DataFrame:
    s = pd.read_excel(path)
    required = ["Day", "STD", "STA", "STD_sin", "STA_sin", "Eqp", "Flt Num", "From", "To"]
    missing = [c for c in required if c not in s.columns]
    if missing:
        raise ValueError(f"schedule is missing columns: {missing}")

    for c in ("Day", "STD", "STA", "STD_sin", "STA_sin"):
        s[c] = pd.to_datetime(s[c], errors="coerce")
        if s[c].isna().any():
            raise ValueError(f"schedule column {c} contains invalid/missing values")

    s["Day"] = s["Day"].dt.normalize()
    s["Eqp"] = s["Eqp"].astype(str).str.strip()
    s["flight_id"] = s["Flt Num"].astype(int).astype(str) + "_" + s["STD"].dt.strftime("%Y%m%dT%H%M%S")
    if s["flight_id"].duplicated().any():
        raise ValueError("schedule contains duplicate flight_id values")
    return s.sort_values(["STD_sin", "flight_id"]).reset_index(drop=True)


def load_pairing_report(path: Path) -> pd.DataFrame:
    p = pd.read_excel(path)
    required = ["Pairing ID", "Flight Date", "Flight Num", "STD_local", "Duty Start"]
    missing = [c for c in required if c not in p.columns]
    if missing:
        raise ValueError(f"pairing_report is missing columns: {missing}")
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
        raise ValueError("pairing_report contains invalid Flight Date values")
    return p


def load_fdp(path: Path) -> dict:
    df = pd.read_csv(path, encoding="utf-8-sig")
    return {
        str(r["time_window"]).strip(): [float(r[f"limit{i}"]) for i in range(1, 9)]
        for _, r in df.iterrows()
    }


def load_costs(path: Path) -> dict:
    df = pd.read_csv(path, encoding="utf-8-sig")
    return dict(zip(df["parameter"].astype(str).str.strip(), df["value"]))


def derive_airport_to_country(schedule: pd.DataFrame) -> dict:
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


def derive_layover_stations(pairing_report: pd.DataFrame, hub: str) -> list[str]:
    stations = set()
    for _, g in pairing_report.groupby("Pairing ID", sort=False):
        g = g.reset_index(drop=True)
        for i in range(1, len(g)):
            same_station = str(g.loc[i - 1, "To"]).strip().upper() == str(g.loc[i, "From"]).strip().upper()
            if pd.notna(g.loc[i, "Duty Start"]) and same_station and str(g.loc[i, "From"]).strip().upper() != hub:
                stations.add(str(g.loc[i, "From"]).strip().upper())
    return sorted(stations)


def report_key(row) -> str:
    t = pd.Timestamp(row["STD_local"])
    return str(int(row["Flight Num"])) + "_" + pd.Timestamp(row["Flight Date"]).strftime("%Y%m%dT") + t.strftime("%H%M%S")


def group_report(report: pd.DataFrame):
    out = []
    for pid, g in report.groupby("Pairing ID", sort=False):
        g = g.reset_index(drop=True).copy()
        g["_key"] = [report_key(r) for _, r in g.iterrows()]
        g["_duty_start"] = g["Duty Start"].notna()
        out.append((str(int(pid)), g))
    return out


# --------------------------------------------------------------------------------------
# MGT through ORIGINAL MGT.py
# --------------------------------------------------------------------------------------

def attach_mgt(df: pd.DataFrame, mgt_module, mgt_xlsx: Path, airport_to_country: dict, subtypes: list[str]) -> pd.DataFrame:
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
# Seeded disruption
# --------------------------------------------------------------------------------------

def simulate_disruption(base: pd.DataFrame, retime_choices: list[int], seed: int, horizon_tag: str) -> pd.DataFrame:
    d = base.copy().reset_index(drop=True)
    d["active"] = True
    d["disruption_status"] = "UNCHANGED"
    d["disruption_type"] = "NONE"
    for c in ("STD", "STA", "STD_sin", "STA_sin", "Eqp"):
        d["original_" + c] = d[c]

    n = int(round(len(d) * DISRUPTION_RATE))
    if n == 0 and len(d) > 0:
        n = 1

    # Stable independent stream per horizon.
    tag_offset = {"1W": 1, "1M": 2}[horizon_tag]
    rng = np.random.default_rng([seed, tag_offset])
    candidates = d.sort_values("flight_id").index.to_numpy()
    chosen = rng.permutation(candidates)[:n]

    eqp_available = d["Eqp"].dropna().astype(str).str.strip().unique().tolist()
    mix = [("CANCELLED", 0.25), ("EQUIPMENT_CHANGED", 0.25), ("RETIMED", 0.50)]
    target_cancel = int(round(n * mix[0][1]))
    target_eqp = int(round(n * mix[1][1])) if len(eqp_available) > 1 else 0
    if target_cancel + target_eqp > n:
        target_eqp = max(0, n - target_cancel)

    for k, idx in enumerate(chosen):
        if k < target_cancel:
            d.at[idx, "active"] = False
            d.at[idx, "disruption_status"] = "CANCELLED"
            d.at[idx, "disruption_type"] = "CANCELLATION"
        elif k < target_cancel + target_eqp:
            old = str(d.at[idx, "Eqp"]).strip()
            alternatives = [e for e in eqp_available if e != old]
            new = str(rng.choice(alternatives))
            d.at[idx, "Eqp"] = new
            d.at[idx, "disruption_status"] = "EQUIPMENT_CHANGED"
            d.at[idx, "disruption_type"] = f"EQP_{old}>{new}"
        else:
            mins = int(rng.choice(retime_choices))
            delta = timedelta(minutes=mins)
            for c in ("STD", "STA", "STD_sin", "STA_sin"):
                d.at[idx, c] = pd.Timestamp(d.at[idx, c]) + delta
            d.at[idx, "disruption_status"] = "RETIMED"
            d.at[idx, "disruption_type"] = f"RETIME_+{mins}MIN"

    # Critical: retiming can change chronological order.
    return d.sort_values(["STD_sin", "flight_id"]).reset_index(drop=True)


# --------------------------------------------------------------------------------------
# Exact reuse of ORIGINAL builder rule functions for validation
# --------------------------------------------------------------------------------------

class RuleChecker:
    def __init__(self, pb, fdp_limits, duty_start_buffer, fdp_end_buffer, duty_end_buffer,
                 max_layover_hours, layover_stations):
        self.pb = pb
        self.fdp = fdp_limits
        self.dsb = duty_start_buffer
        self.feb = fdp_end_buffer
        self.deb = duty_end_buffer
        self.max_layover = timedelta(hours=max_layover_hours)
        self.stations = set(layover_stations)
        self.hub = pb.HUB

    def check_duty(self, duty: list[dict]) -> list[dict]:
        pb = self.pb
        out = []
        if not duty:
            return out

        ids = [f["flight_id"] for f in duty]
        if len(ids) != len(set(ids)):
            out.append({"flight_id": ids[-1], "type": "DUPLICATE_FLIGHT", "rule": "Each flight appears once per pairing",
                        "value": len(ids), "limit": 1, "unit": "count",
                        "explanation": "Duplicate flight ID inside one duty."})
            return out

        if len(duty) > pb.MAX_SECTORS_PER_DUTY:
            out.append({"flight_id": ids[-1], "type": "SECTOR_LIMIT", "rule": "MAX_SECTORS_PER_DUTY",
                        "value": len(duty), "limit": pb.MAX_SECTORS_PER_DUTY, "unit": "sectors",
                        "explanation": "Duty exceeds the original builder's sector cap."})

        duty_start = duty[0]["STD"] - timedelta(minutes=self.dsb)
        duty_start_local = duty[0]["STD_local"] - timedelta(minutes=self.dsb)
        duty_end = duty[-1]["STA"] + timedelta(minutes=(self.deb if len(duty) >= pb.MAX_SECTORS_PER_DUTY else self.feb))
        total_hours = (duty_end - duty_start).total_seconds() / 3600.0
        band = pb.get_time_band_key(duty_start_local)
        limits = self.fdp.get(band)
        if limits is None:
            out.append({"flight_id": ids[-1], "type": "MISSING_FDP_BAND", "rule": "fdp_rules.csv time band",
                        "value": band, "limit": "configured", "unit": "",
                        "explanation": f"No FDP limits found for duty start band {band}."})
        else:
            limit = limits[min(len(duty) - 1, 7)]
            if total_hours > limit + 1e-9:
                out.append({"flight_id": ids[-1], "type": "FDP_LIMIT", "rule": f"FDP limit ({band})",
                            "value": round(total_hours, 2), "limit": limit, "unit": "hours",
                            "explanation": f"Duty is {total_hours:.2f}h against {limit:.2f}h allowed."})

        for k in range(1, len(duty)):
            prev, cur = duty[k - 1], duty[k]
            if prev["To"] != cur["From"]:
                out.append({"flight_id": cur["flight_id"], "type": "CONNECTIVITY",
                            "rule": "prev.To == next.From", "value": f"{prev['To']} -> {cur['From']}",
                            "limit": "same station", "unit": "",
                            "explanation": "Successive sectors do not connect at the same airport."})
                continue

            actual = cur["STD"] - prev["STA"]
            required = pb._required_mgt(k, prev["MGT_td"], prev["Eqp"], cur["Eqp"])
            if actual < required:
                out.append({"flight_id": cur["flight_id"], "type": "MGT_CONNECTION",
                            "rule": "pairings_builder._required_mgt",
                            "value": round(actual.total_seconds() / 60, 1),
                            "limit": round(required.total_seconds() / 60, 1), "unit": "minutes",
                            "explanation": f"Connection has {actual.total_seconds()/60:.1f} min; requires {required.total_seconds()/60:.1f} min."})

        # The builder's single-day equipment rule applies only to hub-turnaround duties.
        if duty[0]["From"] == self.hub and duty[-1]["To"] == self.hub:
            start_eqp = duty[0]["Eqp"]
            for k in range(1, len(duty)):
                if not pb.eqp_allowed_single_day(k, start_eqp, duty[k - 1]["Eqp"], duty[k]["Eqp"]):
                    out.append({"flight_id": duty[k]["flight_id"], "type": "EQUIPMENT_CONTINUITY",
                                "rule": "pairings_builder.eqp_allowed_single_day",
                                "value": f"{duty[k-1]['Eqp']} -> {duty[k]['Eqp']}",
                                "limit": f"start equipment {start_eqp}", "unit": "",
                                "explanation": "Aircraft type progression violates the original hub-turn equipment rule."})
        return out

    def check_pairing(self, seq: list[dict], duty_splits: list[list[int]]) -> list[dict]:
        out = []
        for f in seq:
            if not bool(f.get("active", True)):
                out.append({"flight_id": f["flight_id"], "type": "CANCELLED_FLIGHT",
                            "rule": "Flight must be active", "value": "CANCELLED", "limit": "ACTIVE", "unit": "",
                            "explanation": "The disruption cancelled this flight."})
        if out:
            return out

        duties = [[seq[i] for i in idxs] for idxs in duty_splits]
        for duty in duties:
            out.extend(self.check_duty(duty))

        for i in range(len(duties) - 1):
            a, b = duties[i], duties[i + 1]
            if a[-1]["To"] != b[0]["From"]:
                out.append({"flight_id": b[0]["flight_id"], "type": "DUTY_BOUNDARY_STATION",
                            "rule": "Duty boundary station continuity",
                            "value": f"{a[-1]['To']} -> {b[0]['From']}", "limit": "same station", "unit": "",
                            "explanation": "Consecutive duties do not meet at the same station."})
                continue

            da = self.pb._build_duty_record(a, self.stations, self.dsb, self.feb, self.deb)
            db = self.pb._build_duty_record(b, self.stations, self.dsb, self.feb, self.deb)
            actual = db["start_time"] - da["end_time"]
            if actual <= timedelta(0):
                out.append({"flight_id": b[0]["flight_id"], "type": "NON_POSITIVE_REST",
                            "rule": "Inter-duty rest > 0", "value": round(actual.total_seconds()/3600, 2),
                            "limit": 0, "unit": "hours", "explanation": "Next duty starts before or at previous duty end."})
                continue
            if actual > self.max_layover:
                out.append({"flight_id": b[0]["flight_id"], "type": "MAX_LAYOVER",
                            "rule": f"Maximum layover ({ASSUMPTION_TAG})",
                            "value": round(actual.total_seconds()/3600, 2),
                            "limit": self.max_layover.total_seconds()/3600, "unit": "hours",
                            "explanation": "Inter-duty rest exceeds the demo layover cap."})
                continue
            if not self.pb.valid_rest(da, db, self.max_layover):
                out.append({"flight_id": b[0]["flight_id"], "type": "REST_MINIMUM",
                            "rule": "pairings_builder.valid_rest/_required_rest",
                            "value": round(actual.total_seconds()/3600, 2),
                            "limit": "original builder rest requirement", "unit": "hours",
                            "explanation": "Inter-duty rest fails the original builder's valid_rest rule."})
        return out

    def generated_split_candidates(self, seq: list[dict], pairing_type: str, station: str | None):
        n = len(seq)
        cap = self.pb.MAX_SECTORS_PER_DUTY
        if pairing_type == "single_day":
            return [[list(range(n))]]
        if pairing_type == "multi_day":
            return [[[0], [1]]] if n == 2 else []
        if pairing_type == "combined_post_layover":
            out = []
            for i in range(1, n):
                if seq[i-1]["To"] == seq[i]["From"] == station and i <= cap and n - i <= cap:
                    out.append([list(range(i)), list(range(i, n))])
            return out
        if pairing_type == "combined_pre_layover":
            out = []
            for b2 in range(3, n):
                if seq[b2-1]["To"] != seq[b2]["From"] or seq[b2]["From"] != station:
                    continue
                if not any(seq[i-1]["To"] == self.hub and seq[i]["From"] == self.hub for i in range(1, b2)):
                    continue
                out.append([list(range(b2)), list(range(b2, n))])
            return out
        return [[list(range(n))]]


# --------------------------------------------------------------------------------------
# Existing pairings
# --------------------------------------------------------------------------------------

_VKEY = {"flight_id": "Flight ID", "type": "Violation Type", "rule": "Rule", "value": "Value",
         "limit": "Limit", "unit": "Unit", "explanation": "Explanation"}


def _norm_violation(v: dict) -> dict:
    """FIX (audit): RuleChecker emits lowercase keys; the sheet expects Title-case columns."""
    return {_VKEY.get(k, k): val for k, val in v.items()}


def validate_existing_pairings(report_groups, record_lookup, checker: RuleChecker, start: pd.Timestamp, end: pd.Timestamp):
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
                    "Value": "not found",
                    "Limit": "present",
                    "Unit": "",
                    "Explanation": "Could not match report flight to the baseline/disrupted schedule."
                })
            rows.append({"Pairing ID": pid, "Status": "INVALID", "Number of Violations": len(missing),
                         "Flight IDs": "|".join(missing), "Disruption Impact": "UNKNOWN"})
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
            for f in seq if (not f.get("active", True)) or f.get("disruption_status") != "UNCHANGED"
        })
        for v in viol:
            violations.append({"Pairing ID": pid, **_norm_violation(v)})

        rows.append({
            "Pairing ID": pid,
            "Status": "INVALID" if viol else "VALID",
            "Number of Violations": len(viol),
            "Flight IDs": "|".join(f["flight_id"] for f in seq),
            "Disruption Impact": "|".join(impacts) if impacts else "NONE",
        })

    return pd.DataFrame(rows), violations, skipped_boundary


# --------------------------------------------------------------------------------------
# Generated pairing extraction
# --------------------------------------------------------------------------------------

def process_generated_pairs(pairs, metadata, checker: RuleChecker, disruption_lookup: dict, horizon_tag: str):
    rows = []
    covered = set()
    exact_sequence_seen = set()
    duplicate_sequences = 0
    independent_failures = 0
    family_counts = Counter()

    for i, (seq, meta) in enumerate(zip(pairs, metadata), start=1):
        ids = [f["flight_id"] for f in seq]
        covered.update(ids)
        seq_key = tuple(ids)
        if seq_key in exact_sequence_seen:
            duplicate_sequences += 1
        exact_sequence_seen.add(seq_key)

        ptype = str(meta.get("pairing_type", "UNKNOWN"))
        family_counts[ptype] += 1

        split_candidates = checker.generated_split_candidates(seq, ptype, meta.get("layover_station"))
        best = None
        if split_candidates:
            for split in split_candidates:
                vv = checker.check_pairing(seq, split)
                if not vv:
                    best = []
                    break
                if best is None or len(vv) < len(best):
                    best = vv
        else:
            best = [{"type": "NO_CONSISTENT_DUTY_SEGMENTATION", "flight_id": ids[0],
                     "rule": f"Generated family {ptype}", "value": None, "limit": None, "unit": "",
                     "explanation": "No duty split matching the builder family could be reconstructed."}]

        failed = bool(best)
        independent_failures += int(failed)
        touched = sorted({disruption_lookup[fid] for fid in ids if disruption_lookup.get(fid, "UNCHANGED") != "UNCHANGED"})

        rows.append({
            "Horizon": horizon_tag,
            "New Pairing ID": f"NEW_{horizon_tag}_{i:07d}",
            "Pairing Type": ptype,
            "Flight IDs": "|".join(ids),
            "Number of Sectors": int(meta.get("num_sectors", len(seq))),
            "Number of Duties (builder metadata)": meta.get("num_duties"),
            "Start": seq[0]["STD"],
            "End": seq[-1]["STA"],
            "Duration Hours": round((seq[-1]["STA"] - seq[0]["STD"]).total_seconds()/3600, 2),
            "Layover Station": meta.get("layover_station") or "",
            "Layover Hours": round(float(meta["layover_hours"]), 2) if meta.get("layover_hours") is not None else None,
            "Equipment Changes": sum(a["Eqp"] != b["Eqp"] for a, b in zip(seq, seq[1:])),
            "Independent Rule Check": "FAIL" if failed else "PASS",
            "Independent Check Reasons": "|".join(sorted({x.get("type", "UNKNOWN") for x in (best or [])})),
            "Touches Disrupted Flight": "|".join(touched) if touched else "NO",
        })

    return rows, covered, family_counts, duplicate_sequences, independent_failures


# --------------------------------------------------------------------------------------
# Workbook output
# --------------------------------------------------------------------------------------

def _autosize(ws, max_width=55):
    from openpyxl.utils import get_column_letter
    for i, col in enumerate(ws.iter_cols(min_row=1, max_row=min(ws.max_row, 300)), start=1):
        width = 10
        for cell in col:
            if cell.value is not None:
                width = max(width, min(max_width, len(str(cell.value)) + 2))
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def write_workbook(path: Path, summary: dict, violations: list[dict], generated: list[dict], disrupted: pd.DataFrame):
    from openpyxl.styles import Font

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    viol_cols = ["Horizon", "Pairing ID", "Flight ID", "Violation Type", "Rule", "Value", "Limit", "Unit", "Explanation", "Disruption Involved"]
    gen_cols = [
        "Horizon", "New Pairing ID", "Pairing Type", "Flight IDs", "Number of Sectors",
        "Number of Duties (builder metadata)", "Start", "End", "Duration Hours", "Layover Station",
        "Layover Hours", "Equipment Changes", "Independent Rule Check", "Independent Check Reasons",
        "Touches Disrupted Flight"
    ]
    schedule_cols = [
        "flight_id", "Day", "Flt Num", "From", "To", "STD", "STA", "STD_sin", "STA_sin", "Eqp",
        "active", "disruption_status", "disruption_type", "original_STD", "original_STA", "original_Eqp"
    ]

    # Write atomically so an interrupted long horizon never leaves a corrupt .xlsx file.
    tmp_path = path.with_name(path.stem + ".tmp.xlsx")
    if tmp_path.exists():
        tmp_path.unlink()
    try:
        with pd.ExcelWriter(tmp_path, engine="openpyxl", datetime_format="yyyy-mm-dd hh:mm", date_format="yyyy-mm-dd") as writer:
            pd.DataFrame([summary]).to_excel(writer, sheet_name="Summary", index=False)
            pd.DataFrame(violations, columns=viol_cols).to_excel(writer, sheet_name="Existing Pairing Violations", index=False)
            pd.DataFrame(generated, columns=gen_cols).to_excel(writer, sheet_name="New Pairings", index=False)
            disrupted[schedule_cols].to_excel(writer, sheet_name="Disrupted Schedule", index=False)

            for ws in writer.book.worksheets:
                for cell in ws[1]:
                    cell.font = Font(bold=True)
                _autosize(ws)
        tmp_path.replace(path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def verify_workbook(path: Path):
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    sheetnames = list(wb.sheetnames)
    ok = sheetnames == EXPECTED_SHEETS
    wb.close()
    if not ok:
        raise RuntimeError(f"Unexpected output sheets in {path}: {sheetnames}")


# --------------------------------------------------------------------------------------
# Output filename
# --------------------------------------------------------------------------------------

def output_filename(tag: str) -> str:
    return {
        "1W": "pairing_experiment_1_week.xlsx",
        "1M": "pairing_experiment_1_month.xlsx",
    }[tag]



def run_column_generation_benchmark(pairs, metadata, active_ids, ctx, tag: str):
    """Optional end-to-end adapter + finite-pool LP benchmark on builder output."""
    import independent_validator as iv
    from column_generation import run_finite_pool_column_generation, solve_restricted_master

    rules = iv.Rules(
        fdp=ctx["fdp"], dsb=ctx["dsb"], feb=ctx["feb"], deb=ctx["deb"],
        max_layover_h=MAX_LAYOVER_HOURS_DEMO,
    )
    built = build_pairing_columns(
        pairs,
        metadata,
        sorted(active_ids),
        validator=independent_pairing_validator(rules, validator_module=iv),
    )
    report = {
        "horizon": tag,
        "active_flights": len(active_ids),
        "builder_candidates": len(pairs),
        "validated_columns": len(built.columns),
        "rejected_candidates": len(built.rejected),
        "duplicate_sequences": built.duplicate_sequences,
        "multi_day_columns": sum(c.is_multi_day for c in built.columns),
        "status": "ADAPTER_ONLY_NO_COLUMNS" if not built.columns else "ADAPTER_VALIDATED",
        "lp_benchmark": None,
    }
    if built.columns:
        universe = list(built.columns)
        # A small seed is used deliberately; pricing scans the full validated
        # pool, while the direct LP provides the finite-universe reference.
        seed = [c for c in universe if not c.is_multi_day][:50]
        if not seed:
            seed = universe[:min(50, len(universe))]
        penalty = float(len(active_ids) + 1)
        cg = run_finite_pool_column_generation(
            sorted(active_ids), universe, initial_columns=seed,
            uncovered_penalty=penalty, batch_size=50, max_iterations=100,
        )
        full = solve_restricted_master(sorted(active_ids), universe, penalty)
        report["lp_benchmark"] = {
            "seed_columns": len(seed),
            "columns_added": len(cg.columns) - len(seed),
            "columns_in_final_master": len(cg.columns),
            "iterations": cg.iterations,
            "converged_over_supplied_pool": cg.converged,
            "stop_reason": cg.stop_reason,
            "column_generation_objective": cg.solution.objective,
            "full_pool_lp_objective": full.objective,
            "objective_matches_full_pool": abs(cg.solution.objective - full.objective) <= 1e-7,
            "uncovered_flights": sum(v > 1e-7 for v in cg.solution.uncovered_values.values()),
        }
    out = OUTPUT_DIR / "column_generation" / f"{tag.lower()}_benchmark.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")
    LOG.info("Column-generation benchmark report: %s", out)
    LOG.info("Column-generation benchmark status: %s", report["status"])
    return report


def run_resource_constrained_pricing(active: pd.DataFrame, active_ids: set[str], ctx: dict, tag: str):
    """Run the on-demand resource-constrained pricing prototype on active schedule rows."""
    import independent_validator as iv
    from crew_recovery.integrated_column_generation import solve_with_pricing
    from crew_recovery.resource_constrained_pricing import ResourceConstrainedPairingPricer

    rules = iv.Rules(
        fdp=ctx["fdp"], dsb=ctx["dsb"], feb=ctx["feb"], deb=ctx["deb"],
        max_layover_h=MAX_LAYOVER_HOURS_DEMO,
    )

    def validate_priced_pairing(seq, duty_splits, pairing_type):
        records = []
        for flight in seq:
            record = dict(flight)
            if record.get("mgt_min") is None and record.get("MGT_td") is not None:
                mgt_td = record["MGT_td"]
                if hasattr(mgt_td, "total_seconds"):
                    record["mgt_min"] = float(mgt_td.total_seconds()) / 60.0
            records.append(record)
        return iv.check_pairing(
            "resource-priced-candidate",
            records,
            [list(split) for split in duty_splits],
            rules,
        )

    pricer = ResourceConstrainedPairingPricer(
        active.to_dict("records"),
        fdp_limits=ctx["fdp"],
        duty_start_buffer=ctx["dsb"],
        fdp_end_buffer=ctx["feb"],
        duty_end_buffer=ctx["deb"],
        max_layover_hours=MAX_LAYOVER_HOURS_DEMO,
        layover_stations=ctx["layover_stations"],
        validator=validate_priced_pairing,
        pairing_cost=1.0,
        hub=pb_hub(ctx),
        max_sectors_per_duty=4,
    )
    started = time.time()
    result = solve_with_pricing(
        sorted(active_ids),
        pricer,
        uncovered_penalty=float(len(active_ids) + 1),
        batch_size=50,
        max_iterations=100,
    )
    runtime = time.time() - started
    report = {
        "horizon": tag,
        "active_flights": len(active_ids),
        "pricing_domain": "single-duty hub turnaround and two-duty hub-layover-hub pairings",
        "runner_note": "The experiment runner still executes the canonical builder for its workbook; the pricing oracle itself does not consume or retain that builder pairing universe.",
        "max_sectors_per_duty": 4,
        "max_actual_duties_per_pairing": 2,
        "layover_stations": list(ctx["layover_stations"]),
        "iterations": result.iterations,
        "converged_over_declared_pricing_domain": result.converged,
        "stop_reason": result.stop_reason,
        "pricing_status": result.pricing_status,
        "final_columns": len(result.columns),
        "single_day_columns": sum(not c.is_multi_day for c in result.columns),
        "multi_day_columns": sum(c.is_multi_day for c in result.columns),
        "objective": result.solution.objective,
        "uncovered_flights_with_positive_slack": sum(
            value > 1e-7 for value in result.solution.uncovered_values.values()
        ),
        "total_uncovered_slack": sum(result.solution.uncovered_values.values()),
        "runtime_seconds": round(runtime, 3),
        "history": [dict(item) for item in result.history],
    }
    out = OUTPUT_DIR / "column_generation" / f"{tag.lower()}_resource_constrained_pricing.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")
    LOG.info("Resource-constrained pricing report: %s", out)
    LOG.info("Resource-constrained pricing status: %s (%s)", result.pricing_status, result.stop_reason)
    return report


def pb_hub(ctx: dict) -> str:
    """Return the hub configured by the canonical builder loaded for this run."""
    return str(ctx["pb"].HUB)


# --------------------------------------------------------------------------------------
# Single horizon
# --------------------------------------------------------------------------------------

def run_horizon(tag: str, name: str, base: pd.DataFrame, report_groups, ctx: dict, start: pd.Timestamp, end: pd.Timestamp):
    horizon_start_time = time.time()
    pb = ctx["pb"]
    mgt = ctx["mgt"]
    base = base.copy()

    # The horizon is valid only if the source contains the complete requested calendar period.
    source_end = ctx["source_end"]
    if end > source_end:
        summary = {
            "Horizon": name,
            "Start Date": start.date(),
            "Requested End Date": end.date(),
            "Actual Source End": source_end.date(),
            "Source Flights": len(base),
            "Disrupted Flights": None,
            "Disruption %": None,
            "Existing Pairings Checked": None,
            "Existing Pairings Valid": None,
            "Existing Pairings Invalid": None,
            "Total Violations": None,
            "New Pairings Generated": None,
            "Generated Single-Day": None,
            "Generated Multi-Day": None,
            "Generated Combined Post-Layover": None,
            "Generated Combined Pre-Layover": None,
            "Flights Covered by New Pairings": None,
            "Flights Covered by Existing Valid": None,
            "Flights Covered by Existing Valid + New": None,
            "Flights Uncovered": None,
            "Generated Independent Check Failures": None,
            "Duplicate Generated Sequences": None,
            "Pairing Generation Runtime (s)": None,
            "Total Runtime (s)": round(time.time() - horizon_start_time, 2),
            "Status": "INSUFFICIENT SOURCE DATA",
            "Notes": "No flights were fabricated or extrapolated; requested horizon exceeds actual source schedule."
        }
        output = OUTPUT_DIR / output_filename(tag)
        disrupted = base.copy()
        disrupted["active"] = True
        disrupted["disruption_status"] = "NOT_RUN"
        disrupted["disruption_type"] = "NOT_RUN"
        for c in ("STD", "STA", "STD_sin", "STA_sin", "Eqp"):
            disrupted["original_" + c] = disrupted[c]
        write_workbook(output, summary, [], [], disrupted)
        verify_workbook(output)
        return summary

    LOG.info("[%s] Running %s: %s to %s (%d flights)", tag, name, start.date(), end.date(), len(base))

    # 1) Disruption
    disrupted = simulate_disruption(base, ctx["retime_choices"], SEED, tag)
    disruption_mask = disrupted["disruption_status"] != "UNCHANGED"
    n_disrupted = int(disruption_mask.sum())
    disruption_pct = 100.0 * n_disrupted / len(disrupted) if len(disrupted) else 0.0
    if not (MIN_RATE * 100 <= disruption_pct <= MAX_RATE * 100):
        raise RuntimeError(f"Seeded disruption landed at {disruption_pct:.2f}%, outside 5-10%.")

    # 2) Recalculate MGT on disrupted schedule using ORIGINAL MGT.py
    proc = attach_mgt(disrupted, mgt, ctx["mgt_xlsx"], ctx["airport_to_country"], ctx["subtypes"])
    record_lookup = {r["flight_id"]: r for r in proc.to_dict("records")}
    active = proc[proc["active"]].copy().sort_values(["STD", "flight_id"]).reset_index(drop=True)
    active_ids = set(active["flight_id"])

    # 3) Existing pairing validation
    existing_df, violations, skipped_boundary = validate_existing_pairings(
        report_groups,
        record_lookup,
        ctx["checker"],
        start,
        end,
    )
    n_checked = len(existing_df)
    n_valid = int((existing_df["Status"] == "VALID").sum()) if n_checked else 0
    n_invalid = n_checked - n_valid

    existing_valid_cover = set()
    if n_valid:
        for _, r in existing_df[existing_df["Status"] == "VALID"].iterrows():
            existing_valid_cover.update(str(r["Flight IDs"]).split("|"))
    existing_valid_cover &= active_ids

    for v in violations:
        # RuleChecker uses the internal key ``flight_id`` consistently.
        # Keep the workbook column name separate from the internal dict key.
        fid = v.get("flight_id")
        v["Horizon"] = name
        v["Disruption Involved"] = (
            proc.loc[proc["flight_id"] == fid, "disruption_status"].iloc[0]
            if fid is not None and (proc["flight_id"] == fid).any() else "UNKNOWN"
        )

    # 4) ORIGINAL pairings builder on active disrupted schedule
    layover_df = pd.DataFrame({"Station": ctx["layover_stations"]})
    builder_start = time.time()
    try:
        pairs, metadata = pb.build_all_pairings(
            active,
            ctx["fdp"],
            ctx["dsb"],
            ctx["feb"],
            ctx["deb"],
            MAX_LAYOVER_HOURS_DEMO,
            layover_df,
            MAX_LAYOVER_COMBINED_DEMO,
            start.date(),
            end.date(),
        )
    except Exception as exc:
        raise RuntimeError(f"Original pairings_builder failed for {name}: {type(exc).__name__}: {exc}") from exc
    generation_runtime = time.time() - builder_start

    if ctx.get("run_column_generation_benchmark", False):
        run_column_generation_benchmark(pairs, metadata, active_ids, ctx, tag)
    if ctx.get("run_resource_constrained_pricing", False):
        run_resource_constrained_pricing(active, active_ids, ctx, tag)

    disruption_lookup = {
        r["flight_id"]: ("CANCELLED" if not r["active"] else r["disruption_status"])
        for r in proc.to_dict("records")
    }
    generated_rows, generated_cover, family_counts, duplicate_sequences, independent_failures = process_generated_pairs(
        pairs, metadata, ctx["checker"], disruption_lookup, tag
    )
    generated_cover &= active_ids
    combined_cover = existing_valid_cover | generated_cover
    uncovered = active_ids - combined_cover

    summary = {
        "Horizon": name,
        "Start Date": start.date(),
        "Requested End Date": end.date(),
        "Actual Source End": ctx["source_end"].date(),
        "Source Flights": len(base),
        "Disrupted Flights": n_disrupted,
        "Disruption %": round(disruption_pct, 2),
        "Existing Pairings Checked": n_checked,
        "Existing Pairings Valid": n_valid,
        "Existing Pairings Invalid": n_invalid,
        "Total Violations": len(violations),
        "New Pairings Generated": len(generated_rows),
        "Generated Single-Day": family_counts.get("single_day", 0),
        "Generated Multi-Day": family_counts.get("multi_day", 0),
        "Generated Combined Post-Layover": family_counts.get("combined_post_layover", 0),
        "Generated Combined Pre-Layover": family_counts.get("combined_pre_layover", 0),
        "Flights Covered by New Pairings": len(generated_cover),
        "Flights Covered by Existing Valid": len(existing_valid_cover),
        "Flights Covered by Existing Valid + New": len(combined_cover),
        "Flights Uncovered": len(uncovered),
        "Generated Independent Check Failures": independent_failures,
        "Duplicate Generated Sequences": duplicate_sequences,
        "Pairing Generation Runtime (s)": round(generation_runtime, 2),
        "Total Runtime (s)": round(time.time() - horizon_start_time, 2),
        "Status": "OK",
        "Notes": (
            f"Seed={SEED}; disruption rate={DISRUPTION_RATE:.0%}; {ASSUMPTION_TAG}; "
            f"layover stations reconstructed from pairing_report={ctx['layover_stations']}; "
            f"max layover={MAX_LAYOVER_HOURS_DEMO}h; max combined layover={MAX_LAYOVER_COMBINED_DEMO}h; "
            f"{skipped_boundary} existing pairings crossed the horizon boundary and were excluded from validation."
        ),
    }

    # Output only the fields useful for the experiment.
    disrupted_out = disrupted.copy()
    output = OUTPUT_DIR / output_filename(tag)
    write_workbook(output, summary, violations, generated_rows, disrupted_out)
    verify_workbook(output)

    return summary


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run the planned-schedule -> disruption -> pairing validation -> original builder experiment.")
    ap.add_argument(
        "--horizons", default="1W,1M",
        help="Comma-separated horizons to run: 1W, 1M. Default: both.",
    )
    ap.add_argument(
        "--column-generation-benchmark", action="store_true",
        help="Adapt independently validated builder output into pairing columns and compare finite-pool CG against the full-pool LP. May be expensive for 1M.",
    )
    ap.add_argument(
        "--resource-constrained-pricing", action="store_true",
        help="Run exact on-demand pricing over single-duty and two-duty hub-layover-hub pairings. May be expensive; exactness is limited to the declared pricing domain.",
    )
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", datefmt="%H:%M:%S")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    wanted = {x.strip().upper() for x in args.horizons.split(",") if x.strip()}
    valid_tags = {tag for _, tag, _ in HORIZONS}
    unknown = wanted - valid_tags
    if unknown:
        raise ValueError(f"Unknown horizon(s): {sorted(unknown)}. Use 1W, 1M.")
    if not wanted:
        raise ValueError("No horizon selected. Use 1W, 1M.")

    # IMPORTANT: the original pairings_builder uses multiprocessing.Pool.
    # Running two builder horizons inside the same Python process can deadlock or
    # become unstable after the first Pool is torn down. Therefore the combined
    # mode runs each requested horizon in a fresh child process. Each child executes
    # the exact same original MGT + pairings_builder workflow as the single-horizon run.
    if len(wanted) > 1:
        for tag in ("1W", "1M"):
            if tag not in wanted:
                continue
            LOG.info("Starting isolated %s subprocess", tag)
            child_cmd = [sys.executable, str(Path(__file__).resolve()), "--horizons", tag]
            if args.column_generation_benchmark:
                child_cmd.append("--column-generation-benchmark")
            if args.resource_constrained_pricing:
                child_cmd.append("--resource-constrained-pricing")
            subprocess.run(child_cmd, check=True)
        print("\nCombined experiment completed successfully.")
        print(f"  {OUTPUT_DIR / output_filename('1W')}")
        print(f"  {OUTPUT_DIR / output_filename('1M')}")
        return 0

    # Only these seven source files are required for this experiment.
    mgt_path = original_file("MGT.py")
    builder_path = original_file("pairings_builder.py")
    schedule_path = input_file("schedule.xlsx")
    pairing_report_path = input_file("pairing_report.xlsx")
    mgt_xlsx_path = original_file("MGT.xlsx")
    fdp_path = original_file("fdp_rules.csv")
    cost_path = original_file("cost_parameters.csv")

    LOG.info("MGT.py: %s", mgt_path)
    LOG.info("pairings_builder.py: %s", builder_path)
    LOG.info("schedule.xlsx: %s", schedule_path)
    LOG.info("pairing_report.xlsx: %s", pairing_report_path)

    mgt = load_module(mgt_path, "MGT_original")
    # IMPORTANT: the supplied builder must be registered under its real import name.
    # On Windows/spawn, multiprocessing workers pickle builder functions by module name.
    # Using "pairings_builder_original" here causes workers to look for a non-existent
    # pairings_builder_original.py. The actual supplied file is pairings_builder.py.
    pb = load_module(builder_path, "pairings_builder")
    LOG.info("Builder execution mode: %s", prepare_builder(pb, builder_path))

    required_mgt_functions = ["apply_mgt_processing", "resolve_mgt_minutes"]
    required_builder_functions = [
        "build_all_pairings", "_build_duty_record", "_required_mgt", "valid_rest",
        "valid_sin_connection", "eqp_allowed_single_day", "get_time_band_key"
    ]
    for fn in required_mgt_functions:
        if not hasattr(mgt, fn):
            raise RuntimeError(f"Original MGT.py is missing required function: {fn}")
    for fn in required_builder_functions:
        if not hasattr(pb, fn):
            raise RuntimeError(f"Original pairings_builder.py is missing required function: {fn}")

    schedule = load_schedule(schedule_path)
    report = load_pairing_report(pairing_report_path)
    fdp = load_fdp(fdp_path)
    costs = load_costs(cost_path)

    dsb = int(float(costs["duty_start_buffer"]))
    feb = int(float(costs["fdp_end_buffer"]))
    deb = int(float(costs["reported_duty_end_buffer"]))
    retime_choices = DEFAULT_RETIME_MINUTES + [
        int(float(costs[k])) for k in ("retime_duration_tier_1", "retime_duration_tier_2", "retime_duration_tier_3") if k in costs
    ]
    retime_choices = sorted(set(retime_choices))

    source_start = schedule["Day"].min()
    source_end = schedule["Day"].max()
    layover_stations = derive_layover_stations(report, pb.HUB)
    airport_to_country = derive_airport_to_country(schedule)
    subtypes = sorted(schedule["Eqp"].astype(str).str.upper().unique())

    checker = RuleChecker(
        pb,
        fdp,
        dsb,
        feb,
        deb,
        MAX_LAYOVER_HOURS_DEMO,
        layover_stations,
    )

    ctx = {
        "mgt": mgt,
        "pb": pb,
        "mgt_xlsx": mgt_xlsx_path,
        "fdp": fdp,
        "dsb": dsb,
        "feb": feb,
        "deb": deb,
        "retime_choices": retime_choices,
        "source_end": source_end,
        "airport_to_country": airport_to_country,
        "subtypes": subtypes,
        "layover_stations": layover_stations,
        "checker": checker,
        "run_column_generation_benchmark": args.column_generation_benchmark,
        "run_resource_constrained_pricing": args.resource_constrained_pricing,
        "pb": pb,
    }
    groups = group_report(report)

    LOG.info("Source schedule: %s to %s | %d flights | %d days", source_start.date(), source_end.date(), len(schedule), schedule["Day"].nunique())
    LOG.info("Demo layover stations reconstructed from pairing report: %s", layover_stations)

    summaries = []
    for name, tag, horizon_spec in HORIZONS:
        if tag not in wanted:
            continue
        if tag == "1W":
            end = source_start + pd.Timedelta(days=6)
        else:
            end = source_start + pd.DateOffset(months=horizon_spec) - pd.Timedelta(days=1)
        base = schedule[schedule["Day"].between(source_start, min(end, source_end))].copy()
        summaries.append(run_horizon(tag, name, base, groups, ctx, source_start, end))

    print("\n" + "=" * 112)
    print("HORIZON | SOURCE FLIGHTS | DISRUPTED | EXISTING VALID | EXISTING INVALID | NEW PAIRINGS | COMBINED COVER | UNCOVERED | STATUS")
    print("-" * 112)
    for s in summaries:
        print(
            f"{str(s['Horizon']):8s} | {str(s['Source Flights']):13s} | {str(s['Disrupted Flights']):9s} | "
            f"{str(s['Existing Pairings Valid']):14s} | {str(s['Existing Pairings Invalid']):16s} | "
            f"{str(s['New Pairings Generated']):12s} | {str(s['Flights Covered by Existing Valid + New']):13s} | "
            f"{str(s['Flights Uncovered']):9s} | {s['Status']}"
        )
    print("=" * 112)
    print(f"Output directory: {OUTPUT_DIR}")
    print("Files created/updated:")
    for _, tag, _ in HORIZONS:
        if tag in wanted:
            print(f"  {OUTPUT_DIR / output_filename(tag)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
