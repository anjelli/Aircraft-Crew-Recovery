#!/usr/bin/env python3
"""
Stage-5 coverage MIP for the Scoot crew-pairing experiment.

Runs the same generic coverage MIP independently for:
    1 week
    30 days
    166 days

IMPORTANT
---------
This is a generic candidate-pool MIP for Step 5.

It is NOT Rajdeep's column-generation + aircraft-network-timeline solver.
It does NOT model individual aircraft-tail trackability, crew rostering,
or final operational feasibility.

No changes are made to MGT.py or pairings_builder.py.

INPUTS
------
output/pairing_experiment_1_week.xlsx
output/pairing_experiment_1_month.xlsx
output/pairing_experiment_166_days.xlsx
input/pairing_report.xlsx

CANDIDATE POOL
--------------
Existing valid pairings reconstructed from pairing_report.xlsx
+
New generated pairings from the experiment workbook

Generated pairings are admitted only when:
    Independent Rule Check == PASS

MODEL
-----
Binary x_p = 1 if candidate pairing p is selected.
Binary u_f = 1 if active flight f is left uncovered.

For every active flight f:
    sum_p A[f,p] * x_p + u_f = 1

Objective:
    MIN  M * sum_f u_f + sum_p x_p

where:
    M = number_of_active_flights + 1

This makes the objective coverage-first, then minimizes the number
of selected pairings among equally-covered solutions.

HORIZON HANDLING
----------------
The experiment workbook may use either:
    Actual Source End
    Requested End Date
    End Date

The code handles all three and never fabricates flights.

EXISTING PAIRING MATCHING
-------------------------
The old implementation reconstructed report flight IDs too narrowly.
This version instead matches each pairing-report row against the
DISRUPTED SCHEDULE using:
    Flight Num + Flight Date + original scheduled STD

That keeps the existing-valid candidates aligned with the same
flight IDs used by the experiment runner.
"""

from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import csc_matrix, eye, hstack


LOG = logging.getLogger("mip")


ROOT = Path(__file__).resolve().parent
INPUT_DIR = ROOT / "input"
OUTPUT_DIR = ROOT / "output"

HORIZON_FILES = [
    ("1W", "1 week", "pairing_experiment_1_week.xlsx"),
    ("1M", "30 days", "pairing_experiment_1_month.xlsx"),
    ("166D", "166 days", "pairing_experiment_166_days.xlsx"),
]

EXPECTED_EXPERIMENT_SHEETS = {
    "Summary",
    "Existing Pairing Violations",
    "New Pairings",
    "Disrupted Schedule",
}


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    source: str
    pairing_type: str
    flight_ids: tuple[str, ...]


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def parse_bool(value, default=False) -> bool:
    if pd.isna(value):
        return default
    s = str(value).strip().upper()
    if s in {"TRUE", "YES", "Y", "1"}:
        return True
    if s in {"FALSE", "NO", "N", "0"}:
        return False
    return default


def normalize_flight_id(value) -> str:
    return str(value).strip()


def normalize_pairing_id(value) -> str:
    if pd.isna(value):
        return ""
    try:
        f = float(value)
        if f.is_integer():
            return str(int(f))
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def normalize_flt_num(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Cannot parse flight number: {value!r}") from exc


def parse_time_on_date(value, date_value) -> pd.Timestamp:
    """
    Convert an Excel time-like value into a timestamp on date_value.

    Handles:
        datetime
        pandas Timestamp
        python time
        strings like HH:MM / HH:MM:SS
        Excel time cells represented as datetime-like values
    """
    date = pd.Timestamp(date_value).normalize()

    if pd.isna(value):
        raise ValueError("Missing STD_local value")

    # Native datetime / Timestamp.
    if isinstance(value, (pd.Timestamp,)):
        ts = value
        return date + pd.Timedelta(
            hours=ts.hour,
            minutes=ts.minute,
            seconds=ts.second,
        )

    # Python datetime/time objects.
    try:
        import datetime as _dt

        if isinstance(value, _dt.datetime):
            return date + pd.Timedelta(
                hours=value.hour,
                minutes=value.minute,
                seconds=value.second,
            )
        if isinstance(value, _dt.time):
            return date + pd.Timedelta(
                hours=value.hour,
                minutes=value.minute,
                seconds=value.second,
            )
    except Exception:
        pass

    # Numeric Excel time fraction.
    if isinstance(value, (int, float, np.integer, np.floating)):
        x = float(value)
        if 0 <= x < 1:
            total_seconds = int(round(x * 24 * 3600))
            return date + pd.Timedelta(seconds=total_seconds)

    # String fallback.
    s = str(value).strip()
    if not s:
        raise ValueError("Empty STD_local value")

    # Try datetime parsing first.
    parsed = pd.to_datetime(s, errors="coerce")
    if not pd.isna(parsed):
        return date + pd.Timedelta(
            hours=parsed.hour,
            minutes=parsed.minute,
            seconds=parsed.second,
        )

    raise ValueError(f"Could not parse STD_local value: {value!r}")


def report_row_key(row: pd.Series) -> tuple[int, pd.Timestamp]:
    """
    Key used to match a pairing-report flight to the disrupted schedule.

    IMPORTANT:
    Use Flight Date + STD_local from pairing_report.xlsx, but match to
    original_STD in the disrupted schedule, because disruptions change
    current STD while flight_id remains the baseline identity.
    """
    flight_date = pd.Timestamp(row["Flight Date"]).normalize()
    std_local = parse_time_on_date(row["STD_local"], flight_date)
    return normalize_flt_num(row["Flight Num"]), std_local


def schedule_row_key(row: pd.Series) -> tuple[int, pd.Timestamp]:
    """
    Build the same key from the disrupted schedule using original_STD.
    """
    flight_num = normalize_flt_num(row["Flt Num"])

    if "original_STD" not in row.index:
        raise ValueError(
            "Disrupted Schedule is missing original_STD; "
            "cannot safely reconstruct existing pairings."
        )

    original_std = pd.Timestamp(row["original_STD"])
    return flight_num, original_std


def horizon_dates(summary: dict) -> tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]:
    """
    Return:
        start_date
        end_date
        requested_end

    Supports multiple summary schemas.
    """
    if "Start Date" not in summary:
        raise ValueError("Summary sheet does not contain 'Start Date'.")

    start_date = pd.Timestamp(summary["Start Date"]).normalize()

    end_candidates = [
        "Actual Source End",
        "Requested End Date",
        "End Date",
        "Source End",
    ]

    requested_end = None
    for col in ("Requested End Date", "End Date", "Actual Source End", "Source End"):
        if col in summary and not pd.isna(summary[col]):
            requested_end = pd.Timestamp(summary[col]).normalize()
            break

    if requested_end is None:
        raise ValueError(
            "Summary sheet has no usable horizon end date. "
            "Expected one of: Actual Source End, Requested End Date, End Date."
        )

    actual_source_end = None
    if "Actual Source End" in summary and not pd.isna(summary["Actual Source End"]):
        actual_source_end = pd.Timestamp(summary["Actual Source End"]).normalize()

    if actual_source_end is None:
        actual_source_end = requested_end

    # Never use a date beyond the source's declared actual end.
    end_date = min(requested_end, actual_source_end)

    return start_date, end_date, requested_end


# ---------------------------------------------------------------------------
# Workbook loading
# ---------------------------------------------------------------------------

def read_experiment_workbook(path: Path):
    if not path.is_file():
        raise FileNotFoundError(f"Missing experiment workbook: {path}")

    xls = pd.ExcelFile(path)
    sheets = set(xls.sheet_names)

    missing = EXPECTED_EXPERIMENT_SHEETS - sheets
    if missing:
        raise ValueError(
            f"{path.name} is missing sheets: {sorted(missing)}"
        )

    summary = pd.read_excel(path, sheet_name="Summary")
    violations = pd.read_excel(
        path, sheet_name="Existing Pairing Violations"
    )
    new_pairings = pd.read_excel(path, sheet_name="New Pairings")
    disrupted = pd.read_excel(path, sheet_name="Disrupted Schedule")

    if summary.empty:
        raise ValueError(f"{path.name}: Summary sheet is empty.")

    return (
        summary.iloc[0].to_dict(),
        violations,
        new_pairings,
        disrupted,
    )


# ---------------------------------------------------------------------------
# Existing pairing reconstruction
# ---------------------------------------------------------------------------

def build_disrupted_lookup(
    disrupted: pd.DataFrame,
) -> dict[tuple[int, pd.Timestamp], dict]:
    """
    Map:
        (flight number, original STD) -> disrupted schedule row dict

    This is deliberately based on ORIGINAL_STD rather than current STD,
    because RETIMED flights have a changed current STD but retain their
    baseline flight identity.
    """
    required = {
        "flight_id",
        "Flt Num",
        "original_STD",
        "active",
    }
    missing = required - set(disrupted.columns)
    if missing:
        raise ValueError(
            f"Disrupted Schedule is missing required columns: "
            f"{sorted(missing)}"
        )

    lookup: dict[tuple[int, pd.Timestamp], dict] = {}

    for _, row in disrupted.iterrows():
        key = schedule_row_key(row)

        if key in lookup:
            raise ValueError(
                f"Duplicate disrupted-schedule key encountered: {key}"
            )

        lookup[key] = row.to_dict()

    return lookup


def collect_invalid_pairing_ids(violations: pd.DataFrame) -> set[str]:
    if "Pairing ID" not in violations.columns:
        return set()

    return {
        normalize_pairing_id(v)
        for v in violations["Pairing ID"].dropna()
    }


def load_existing_pairing_candidates(
    pairing_report_path: Path,
    violations: pd.DataFrame,
    disrupted: pd.DataFrame,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    active_ids: set[str],
) -> tuple[list[Candidate], dict]:
    """
    Reconstruct only existing pairings that are:

      1. not listed as invalid by the experiment,
      2. fully contained inside the horizon,
      3. fully matched to the disrupted schedule,
      4. fully active after disruption,
      5. unique by flight sequence.

    Returns:
        candidates
        diagnostics
    """
    report = pd.read_excel(pairing_report_path)

    required = {
        "Pairing ID",
        "Flight Date",
        "Flight Num",
        "STD_local",
    }
    missing = required - set(report.columns)
    if missing:
        raise ValueError(
            f"{pairing_report_path.name} is missing columns: "
            f"{sorted(missing)}"
        )

    report["Pairing ID"] = report["Pairing ID"].ffill()
    report["Flight Date"] = pd.to_datetime(
        report["Flight Date"], errors="coerce"
    ).dt.normalize()
    report = report.dropna(
        subset=["Pairing ID", "Flight Date"]
    ).copy()

    disrupted_lookup = build_disrupted_lookup(disrupted)
    invalid_ids = collect_invalid_pairing_ids(violations)

    candidates: list[Candidate] = []
    seen_sequences: set[tuple[str, ...]] = set()

    diagnostics = {
        "Report Pairings Total": 0,
        "Invalid Pairing IDs From Experiment": len(invalid_ids),
        "Pairings Outside Horizon": 0,
        "Pairings Crossing Horizon Boundary": 0,
        "Pairings Valid-Status Before Matching": 0,
        "Pairings With Missing Schedule Match": 0,
        "Pairings With Inactive Flight": 0,
        "Duplicate Existing Sequences Removed": 0,
        "Existing Valid Candidates Loaded": 0,
    }

    for pid, group in report.groupby("Pairing ID", sort=False):
        diagnostics["Report Pairings Total"] += 1

        pid_str = normalize_pairing_id(pid)
        if not pid_str:
            continue

        dates = group["Flight Date"].dt.normalize()
        in_horizon = dates.between(start_date, end_date)

        # Mirror the experiment's horizon-contained logic:
        # all report rows must lie inside the target horizon.
        if not bool(in_horizon.any()):
            diagnostics["Pairings Outside Horizon"] += 1
            continue

        if not bool(in_horizon.all()):
            diagnostics["Pairings Crossing Horizon Boundary"] += 1
            continue

        # Any violation row for a pairing ID means the experiment marked
        # that existing pairing invalid.
        if pid_str in invalid_ids:
            continue

        diagnostics["Pairings Valid-Status Before Matching"] += 1

        seq: list[str] = []
        missing_matches: list[str] = []
        inactive_ids: list[str] = []

        for _, row in group.iterrows():
            try:
                key = report_row_key(row)
            except Exception as exc:
                missing_matches.append(
                    f"{row['Flight Num']}_{row['Flight Date']}:PARSE_ERROR:{exc}"
                )
                continue

            matched = disrupted_lookup.get(key)

            if matched is None:
                missing_matches.append(
                    f"{normalize_flt_num(row['Flight Num'])}_"
                    f"{pd.Timestamp(row['Flight Date']).strftime('%Y%m%d')}_"
                    f"{str(row['STD_local']).strip()}"
                )
                continue

            fid = normalize_flight_id(matched["flight_id"])
            seq.append(fid)

            if not parse_bool(matched.get("active"), default=True):
                inactive_ids.append(fid)

        if missing_matches:
            diagnostics["Pairings With Missing Schedule Match"] += 1
            continue

        if inactive_ids:
            diagnostics["Pairings With Inactive Flight"] += 1
            continue

        # Remove accidental duplicate rows without changing order.
        seq_tuple = tuple(dict.fromkeys(seq))

        if not seq_tuple:
            continue

        if seq_tuple in seen_sequences:
            diagnostics["Duplicate Existing Sequences Removed"] += 1
            continue

        # Final safety check: every flight must belong to the active set.
        if any(fid not in active_ids for fid in seq_tuple):
            diagnostics["Pairings With Inactive Flight"] += 1
            continue

        seen_sequences.add(seq_tuple)

        candidates.append(
            Candidate(
                candidate_id=f"OLD_{pid_str}",
                source="existing_valid",
                pairing_type="existing",
                flight_ids=seq_tuple,
            )
        )

    diagnostics["Existing Valid Candidates Loaded"] = len(candidates)

    return candidates, diagnostics


# ---------------------------------------------------------------------------
# Generated candidate loading
# ---------------------------------------------------------------------------

def load_generated_candidates(
    new_pairings: pd.DataFrame,
    active_ids: set[str],
) -> tuple[list[Candidate], dict]:
    required = {
        "New Pairing ID",
        "Pairing Type",
        "Flight IDs",
    }
    missing = required - set(new_pairings.columns)
    if missing:
        raise ValueError(
            f"New Pairings sheet is missing columns: {sorted(missing)}"
        )

    has_validation = "Independent Rule Check" in new_pairings.columns

    candidates: list[Candidate] = []
    seen_sequences: set[tuple[str, ...]] = set()

    diagnostics = {
        "Generated Rows Read": len(new_pairings),
        "Generated Rows Rejected Not PASS": 0,
        "Generated Rows Rejected Bad Sequence": 0,
        "Generated Rows Rejected Inactive/Out-of-Horizon": 0,
        "Duplicate Generated Sequences Removed": 0,
        "Generated PASS Candidates Loaded": 0,
    }

    for _, row in new_pairings.iterrows():
        if has_validation:
            status = str(
                row["Independent Rule Check"]
            ).strip().upper()

            # Only PASS is admitted when the workbook explicitly supplies
            # an independent validation result.
            if status != "PASS":
                diagnostics["Generated Rows Rejected Not PASS"] += 1
                continue

        raw = row["Flight IDs"]
        if pd.isna(raw):
            diagnostics["Generated Rows Rejected Bad Sequence"] += 1
            continue

        seq = tuple(
            normalize_flight_id(x)
            for x in str(raw).split("|")
            if str(x).strip()
        )

        if not seq or len(seq) != len(set(seq)):
            diagnostics["Generated Rows Rejected Bad Sequence"] += 1
            continue

        if any(fid not in active_ids for fid in seq):
            diagnostics[
                "Generated Rows Rejected Inactive/Out-of-Horizon"
            ] += 1
            continue

        if seq in seen_sequences:
            diagnostics["Duplicate Generated Sequences Removed"] += 1
            continue

        seen_sequences.add(seq)

        candidate_id = str(row["New Pairing ID"]).strip()
        pairing_type = str(row["Pairing Type"]).strip()

        candidates.append(
            Candidate(
                candidate_id=candidate_id,
                source="new_generated",
                pairing_type=pairing_type,
                flight_ids=seq,
            )
        )

    diagnostics["Generated PASS Candidates Loaded"] = len(candidates)

    return candidates, diagnostics


# ---------------------------------------------------------------------------
# MIP model
# ---------------------------------------------------------------------------

def build_incidence_matrix(
    active_ids: list[str],
    candidates: list[Candidate],
) -> csc_matrix:
    flight_index = {fid: i for i, fid in enumerate(active_ids)}

    rows: list[int] = []
    cols: list[int] = []
    data: list[float] = []

    for j, candidate in enumerate(candidates):
        for fid in candidate.flight_ids:
            i = flight_index.get(fid)
            if i is None:
                continue
            rows.append(i)
            cols.append(j)
            data.append(1.0)

    return csc_matrix(
        (data, (rows, cols)),
        shape=(len(active_ids), len(candidates)),
        dtype=float,
    )


def solve_coverage_mip(
    active_ids: list[str],
    candidates: list[Candidate],
    time_limit_seconds: float,
):
    n_flights = len(active_ids)
    n_candidates = len(candidates)

    if n_flights == 0:
        raise ValueError("No active flights available for the MIP.")

    coverage = build_incidence_matrix(active_ids, candidates)

    # Decision variables:
    # x = candidate-selection binaries
    # u = uncovered-flight binaries
    n_vars = n_candidates + n_flights

    constraint_matrix = hstack(
        [coverage, eye(n_flights, format="csc", dtype=float)],
        format="csc",
        dtype=float,
    )

    rhs = np.ones(n_flights, dtype=float)

    # Coverage-first objective.
    # Any possible change in number of selected pairings is < M, so one
    # additional uncovered flight is always more expensive.
    big_m = float(n_flights + 1)

    objective = np.concatenate(
        [
            np.ones(n_candidates, dtype=float),
            np.full(n_flights, big_m, dtype=float),
        ]
    )

    integrality = np.ones(n_vars, dtype=int)
    lower = np.zeros(n_vars, dtype=float)
    upper = np.ones(n_vars, dtype=float)

    constraints = LinearConstraint(
        constraint_matrix,
        rhs,
        rhs,
    )

    LOG.info(
        "Building MIP: flights=%d candidates=%d variables=%d",
        n_flights,
        n_candidates,
        n_vars,
    )

    t0 = time.perf_counter()

    result = milp(
        c=objective,
        integrality=integrality,
        bounds=Bounds(lower, upper),
        constraints=constraints,
        options={
            "time_limit": float(time_limit_seconds),
            "presolve": True,
        },
    )

    runtime = time.perf_counter() - t0

    if result.x is None:
        raise RuntimeError(
            "HiGHS returned no solution vector. "
            f"status={getattr(result, 'status', None)}, "
            f"message={getattr(result, 'message', '')}"
        )

    x = (
        np.rint(result.x[:n_candidates]).astype(int)
        if n_candidates
        else np.zeros(0, dtype=int)
    )
    u = np.rint(result.x[n_candidates:]).astype(int)

    selected_indices = np.flatnonzero(x == 1).tolist()
    uncovered_indices = np.flatnonzero(u == 1).tolist()

    selected_candidates = [
        candidates[i] for i in selected_indices
    ]
    uncovered_flights = [
        active_ids[i] for i in uncovered_indices
    ]

    pre_mip_coverage = (
        np.asarray(coverage.sum(axis=1)).ravel() > 0
        if n_candidates
        else np.zeros(n_flights, dtype=bool)
    )

    return {
        "result": result,
        "runtime": runtime,
        "selected_candidates": selected_candidates,
        "uncovered_flights": uncovered_flights,
        "pre_mip_coverage": pre_mip_coverage,
        "coverage_matrix": coverage,
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_solution_workbook(
    path: Path,
    summary: dict,
    selected: list[Candidate],
    flight_rows: list[dict],
):
    selected_rows = [
        {
            "Candidate ID": c.candidate_id,
            "Source": c.source,
            "Pairing Type": c.pairing_type,
            "Flight IDs": "|".join(c.flight_ids),
            "Number of Sectors": len(c.flight_ids),
        }
        for c in selected
    ]

    with pd.ExcelWriter(
        path,
        engine="openpyxl",
        datetime_format="yyyy-mm-dd hh:mm",
        date_format="yyyy-mm-dd",
    ) as writer:
        pd.DataFrame([summary]).to_excel(
            writer,
            sheet_name="MIP Summary",
            index=False,
        )

        pd.DataFrame(selected_rows).to_excel(
            writer,
            sheet_name="Selected Pairings",
            index=False,
        )

        pd.DataFrame(flight_rows).to_excel(
            writer,
            sheet_name="Flight Coverage",
            index=False,
        )

        from openpyxl.styles import Font

        for ws in writer.book.worksheets:
            for cell in ws[1]:
                cell.font = Font(bold=True)

            ws.freeze_panes = "A2"

            for column_cells in ws.columns:
                max_len = 10
                for cell in column_cells[:300]:
                    if cell.value is not None:
                        max_len = max(
                            max_len,
                            min(60, len(str(cell.value)) + 2),
                        )
                ws.column_dimensions[
                    column_cells[0].column_letter
                ].width = max_len


def output_name(tag: str) -> str:
    return {
        "1W": "mip_solution_1_week.xlsx",
        "1M": "mip_solution_1_month.xlsx",
        "166D": "mip_solution_166_days.xlsx",
    }[tag]


# ---------------------------------------------------------------------------
# Single horizon
# ---------------------------------------------------------------------------

def run_one(
    tag: str,
    name: str,
    workbook_path: Path,
    pairing_report_path: Path,
    time_limit_seconds: float,
):
    LOG.info("[%s] Reading %s", tag, workbook_path)

    summary0, violations, new_pairings, disrupted = (
        read_experiment_workbook(workbook_path)
    )

    start_date, end_date, requested_end = horizon_dates(summary0)

    # Warn if the workbook schema suggests a requested horizon beyond source.
    if requested_end > end_date:
        LOG.warning(
            "[%s] Requested end %s exceeds usable source end %s; "
            "using %s and not fabricating flights.",
            tag,
            requested_end.date(),
            end_date.date(),
            end_date.date(),
        )

    if "flight_id" not in disrupted.columns:
        raise ValueError(
            f"{workbook_path.name}: Disrupted Schedule has no flight_id column."
        )

    disrupted["flight_id"] = (
        disrupted["flight_id"]
        .astype(str)
        .str.strip()
    )

    active_mask = disrupted["active"].apply(
        parse_bool,
        default=True,
    )

    active_ids = sorted(
        disrupted.loc[active_mask, "flight_id"]
        .astype(str)
        .map(normalize_flight_id)
        .unique()
    )

    active_id_set = set(active_ids)

    LOG.info(
        "[%s] Horizon=%s to %s | active=%d",
        tag,
        start_date.date(),
        end_date.date(),
        len(active_ids),
    )

    # ------------------------------------------------------------------
    # Existing valid pairing candidates
    # ------------------------------------------------------------------
    existing_candidates, existing_diag = (
        load_existing_pairing_candidates(
            pairing_report_path=pairing_report_path,
            violations=violations,
            disrupted=disrupted,
            start_date=start_date,
            end_date=end_date,
            active_ids=active_id_set,
        )
    )

    LOG.info(
        "[%s] Existing candidates: loaded=%d | report_total=%d | "
        "valid_status_before_matching=%d | missing_match=%d | "
        "inactive=%d | boundary=%d",
        tag,
        len(existing_candidates),
        existing_diag["Report Pairings Total"],
        existing_diag["Pairings Valid-Status Before Matching"],
        existing_diag["Pairings With Missing Schedule Match"],
        existing_diag["Pairings With Inactive Flight"],
        existing_diag["Pairings Crossing Horizon Boundary"],
    )

    # ------------------------------------------------------------------
    # New generated pairing candidates
    # ------------------------------------------------------------------
    generated_candidates, generated_diag = (
        load_generated_candidates(
            new_pairings,
            active_id_set,
        )
    )

    LOG.info(
        "[%s] Generated candidates: rows=%d | PASS loaded=%d | "
        "not PASS=%d | bad=%d | inactive/out-of-horizon=%d | duplicates=%d",
        tag,
        generated_diag["Generated Rows Read"],
        len(generated_candidates),
        generated_diag["Generated Rows Rejected Not PASS"],
        generated_diag["Generated Rows Rejected Bad Sequence"],
        generated_diag[
            "Generated Rows Rejected Inactive/Out-of-Horizon"
        ],
        generated_diag["Duplicate Generated Sequences Removed"],
    )

    # ------------------------------------------------------------------
    # Combine existing valid + generated
    # ------------------------------------------------------------------
    all_candidates = existing_candidates + generated_candidates

    if not all_candidates:
        raise RuntimeError(
            f"[{tag}] No usable candidates remained after filtering."
        )

    # Final de-duplication across OLD + NEW.
    deduped: list[Candidate] = []
    seen_sequences: set[tuple[str, ...]] = set()
    duplicate_pool = 0

    for candidate in all_candidates:
        if candidate.flight_ids in seen_sequences:
            duplicate_pool += 1
            continue

        seen_sequences.add(candidate.flight_ids)
        deduped.append(candidate)

    all_candidates = deduped

    expected_candidate_pool_before_cross_source_dedup = (
        len(existing_candidates) + len(generated_candidates)
    )

    LOG.info(
        "[%s] Candidate pool: existing=%d + generated=%d = %d "
        "before cross-source dedup | final=%d",
        tag,
        len(existing_candidates),
        len(generated_candidates),
        expected_candidate_pool_before_cross_source_dedup,
        len(all_candidates),
    )

    # ------------------------------------------------------------------
    # Solve
    # ------------------------------------------------------------------
    mip = solve_coverage_mip(
        active_ids=active_ids,
        candidates=all_candidates,
        time_limit_seconds=time_limit_seconds,
    )

    result = mip["result"]
    selected = mip["selected_candidates"]
    uncovered = set(mip["uncovered_flights"])
    pre_mip_coverage = mip["pre_mip_coverage"]

    selected_by_flight: dict[str, list[str]] = {
        fid: [] for fid in active_ids
    }

    for candidate in selected:
        for fid in candidate.flight_ids:
            if fid in selected_by_flight:
                selected_by_flight[fid].append(
                    candidate.candidate_id
                )

    covered_after_mip = len(active_ids) - len(uncovered)
    pre_mip_covered = int(pre_mip_coverage.sum())

    status_map = {
        0: "OPTIMAL",
        1: "TIME_LIMIT_OR_ITERATION_LIMIT",
        2: "INFEASIBLE",
        3: "UNBOUNDED",
    }

    solver_status = status_map.get(
        int(getattr(result, "status", -1)),
        f"STATUS_{getattr(result, 'status', -1)}",
    )

    objective = (
        float(result.fun)
        if getattr(result, "fun", None) is not None
        else None
    )

    # ------------------------------------------------------------------
    # Per-flight output
    # ------------------------------------------------------------------
    flight_rows = []

    for i, fid in enumerate(active_ids):
        selected_here = selected_by_flight[fid]

        flight_rows.append(
            {
                "Flight ID": fid,
                "Candidate Covered Before MIP": (
                    "YES" if pre_mip_coverage[i] else "NO"
                ),
                "Selected Candidate(s)": "|".join(selected_here),
                "MIP Status": (
                    "UNCOVERED"
                    if fid in uncovered
                    else "COVERED"
                ),
            }
        )

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    summary = {
        "Horizon": name,
        "Start Date": start_date.date(),
        "End Date Used": end_date.date(),
        "Requested End Date": requested_end.date(),
        "Active Flights": len(active_ids),

        "Existing Valid Candidates": len(existing_candidates),
        "Generated PASS Candidates": len(generated_candidates),
        "Candidate Pool Before Cross-Source Dedup": (
            expected_candidate_pool_before_cross_source_dedup
        ),
        "Candidate Pool After De-duplication": len(all_candidates),

        "Candidate-Covered Before MIP": pre_mip_covered,
        "Candidate Coverage Before MIP %": round(
            100.0 * pre_mip_covered / len(active_ids),
            2,
        ),

        "Selected Pairings": len(selected),
        "Flights Covered After MIP": covered_after_mip,
        "Flights Uncovered After MIP": len(uncovered),
        "MIP Coverage After MIP %": round(
            100.0 * covered_after_mip / len(active_ids),
            2,
        ),

        "Existing Report Pairings Total": existing_diag[
            "Report Pairings Total"
        ],
        "Existing Pairing IDs Marked Invalid": existing_diag[
            "Invalid Pairing IDs From Experiment"
        ],
        "Existing Pairings Crossing Boundary": existing_diag[
            "Pairings Crossing Horizon Boundary"
        ],
        "Existing Pairings Missing Schedule Match": existing_diag[
            "Pairings With Missing Schedule Match"
        ],
        "Existing Pairings Inactive": existing_diag[
            "Pairings With Inactive Flight"
        ],
        "Existing Duplicate Sequences Removed": existing_diag[
            "Duplicate Existing Sequences Removed"
        ],

        "Generated Rows Rejected": (
            generated_diag["Generated Rows Rejected Not PASS"]
            + generated_diag["Generated Rows Rejected Bad Sequence"]
            + generated_diag[
                "Generated Rows Rejected Inactive/Out-of-Horizon"
            ]
        ),
        "Generated Duplicate Sequences Removed": generated_diag[
            "Duplicate Generated Sequences Removed"
        ],
        "Cross-Source Duplicate Sequences Removed": duplicate_pool,

        "Solver Status": solver_status,
        "Solver Message": str(
            getattr(result, "message", "")
        ),
        "Objective": objective,
        "MIP Runtime (s)": round(mip["runtime"], 2),
        "MIP Nodes": getattr(result, "mip_node_count", None),
        "MIP Gap": getattr(result, "mip_gap", None),
        "MIP Dual Bound": getattr(
            result, "mip_dual_bound", None
        ),

        "Model": (
            "Coverage-first binary set-partitioning with "
            "uncovered-flight slack"
        ),
        "Aircraft Trackability": "NOT modeled",
        "Crew Roster Feasibility": "NOT modeled",
        "Operational Cost": (
            "NOT modeled; secondary objective minimizes "
            "selected pairings"
        ),
    }

    out_path = OUTPUT_DIR / output_name(tag)

    write_solution_workbook(
        path=out_path,
        summary=summary,
        selected=selected,
        flight_rows=flight_rows,
    )

    LOG.info(
        "[%s] %s | active=%d | existing=%d | generated=%d | "
        "candidate_pool=%d | selected=%d | uncovered=%d | runtime=%.2fs",
        tag,
        solver_status,
        len(active_ids),
        len(existing_candidates),
        len(generated_candidates),
        len(all_candidates),
        len(selected),
        len(uncovered),
        mip["runtime"],
    )

    return summary, out_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Run the Stage-5 coverage MIP for "
            "1W, 1M and 166D."
        )
    )

    parser.add_argument(
        "--time-limit",
        type=float,
        default=300.0,
        help=(
            "HiGHS time limit per horizon in seconds. "
            "Default: 300."
        ),
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    pairing_report_path = INPUT_DIR / "pairing_report.xlsx"

    if not pairing_report_path.is_file():
        matches = sorted(INPUT_DIR.glob("pairing_report*.xlsx"))
        if not matches:
            raise FileNotFoundError(
                f"Could not find pairing_report.xlsx in {INPUT_DIR}"
            )
        pairing_report_path = matches[-1]

    LOG.info(
        "Using existing pairing report: %s",
        pairing_report_path,
    )

    summaries = []

    for tag, name, filename in HORIZON_FILES:
        experiment_path = OUTPUT_DIR / filename

        summary, out_path = run_one(
            tag=tag,
            name=name,
            workbook_path=experiment_path,
            pairing_report_path=pairing_report_path,
            time_limit_seconds=args.time_limit,
        )

        summaries.append(summary)

    # ------------------------------------------------------------------
    # Combined summary
    # ------------------------------------------------------------------
    summary_path = OUTPUT_DIR / "mip_three_horizons_summary.xlsx"

    summary_df = pd.DataFrame(summaries)

    with pd.ExcelWriter(
        summary_path,
        engine="openpyxl",
    ) as writer:
        summary_df.to_excel(
            writer,
            sheet_name="Summary",
            index=False,
        )

        from openpyxl.styles import Font

        ws = writer.book["Summary"]

        for cell in ws[1]:
            cell.font = Font(bold=True)

        ws.freeze_panes = "A2"

        for column_cells in ws.columns:
            max_len = 10
            for cell in column_cells[:100]:
                if cell.value is not None:
                    max_len = max(
                        max_len,
                        min(60, len(str(cell.value)) + 2),
                    )

            ws.column_dimensions[
                column_cells[0].column_letter
            ].width = max_len

    # ------------------------------------------------------------------
    # Console summary
    # ------------------------------------------------------------------
    print("\n" + "=" * 150)
    print(
        "HORIZON | ACTIVE | EXISTING VALID | GENERATED PASS | "
        "CANDIDATES | PRE-MIP COVERAGE | SELECTED | POST-MIP COVERAGE | "
        "UNCOVERED | STATUS"
    )
    print("-" * 150)

    for s in summaries:
        print(
            f"{str(s['Horizon']):8s} | "
            f"{s['Active Flights']:6d} | "
            f"{s['Existing Valid Candidates']:14d} | "
            f"{s['Generated PASS Candidates']:14d} | "
            f"{s['Candidate Pool After De-duplication']:10d} | "
            f"{s['Candidate Coverage Before MIP %']:15.2f}% | "
            f"{s['Selected Pairings']:8d} | "
            f"{s['MIP Coverage After MIP %']:16.2f}% | "
            f"{s['Flights Uncovered After MIP']:9d} | "
            f"{s['Solver Status']}"
        )

    print("=" * 150)
    print(f"MIP summary: {summary_path}")
    print(f"1W solution:   {OUTPUT_DIR / 'mip_solution_1_week.xlsx'}")
    print(f"1M solution:   {OUTPUT_DIR / 'mip_solution_1_month.xlsx'}")
    print(f"166D solution: {OUTPUT_DIR / 'mip_solution_166_days.xlsx'}")


if __name__ == "__main__":
    main()
