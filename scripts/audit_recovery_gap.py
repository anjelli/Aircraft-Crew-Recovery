"""Consolidate one-week recovery gaps across builder, pricing, and source audits.

This is a read-only audit. It does not infer operational infeasibility or modify
canonical inputs. Supply the exact artifacts from the experiment being reviewed.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd


def _read_csv(path: Path, required: set[str]) -> pd.DataFrame:
    frame = pd.read_csv(path)
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"{path}: missing required columns {missing}")
    return frame


def _sequence(value: Any) -> str:
    if pd.isna(value):
        return ""
    return "|".join(part.strip() for part in str(value).split("|") if part.strip())


def _flight_sequence_map(workbook: Path) -> pd.DataFrame:
    pairings = pd.read_excel(workbook, sheet_name="New Pairings")
    required = {"New Pairing ID", "Pairing Type", "Flight IDs", "Independent Rule Check"}
    missing = sorted(required - set(pairings.columns))
    if missing:
        raise ValueError(f"{workbook}: New Pairings sheet missing {missing}")
    pairings = pairings.copy()
    pairings["sequence"] = pairings["Flight IDs"].map(_sequence)
    pairings["source_pairing_id"] = pairings["New Pairing ID"].astype(str)
    pairings["pairing_type"] = pairings["Pairing Type"].astype(str)
    pairings["independent_check"] = pairings["Independent Rule Check"].astype(str)
    return pairings[["source_pairing_id", "pairing_type", "sequence", "independent_check"]]


def audit_uncovered(coverage: pd.DataFrame, columns: pd.DataFrame) -> pd.DataFrame:
    """Classify positive-slack flights without claiming operational infeasibility."""
    seqs: dict[str, list[str]] = {}
    for row in columns.itertuples(index=False):
        sequence = _sequence(getattr(row, "flight_ids"))
        for flight_id in sequence.split("|") if sequence else []:
            seqs.setdefault(flight_id, []).append(str(getattr(row, "column_id")))

    rows = coverage.loc[pd.to_numeric(coverage["lp_uncovered_slack"], errors="coerce") > 1e-8].copy()
    rows["candidate_column_ids"] = rows["flight_id"].map(
        lambda fid: "|".join(sorted(seqs.get(str(fid), [])))
    )
    rows["triage_class"] = rows["candidate_columns_covering_flight"].fillna(0).astype(int).map(
        lambda n: "CANDIDATE_EXISTS_NOT_SELECTED" if n > 0 else "NO_PRICING_CANDIDATE"
    )
    rows["diagnosis_status"] = "NEEDS_FLIGHT_LEVEL_RULE_AND_BOUNDARY_REVIEW"
    return rows.sort_values(["triage_class", "flight_id"]).reset_index(drop=True)


def audit_sequence_universes(builder: pd.DataFrame, columns: pd.DataFrame) -> pd.DataFrame:
    builder_seq = set(builder["sequence"]) - {""}
    pricing_seq = {_sequence(v) for v in columns["flight_ids"]} - {""}
    all_sequences = sorted(builder_seq | pricing_seq)
    builder_lookup = builder.groupby("sequence", dropna=False).agg(
        builder_count=("source_pairing_id", "size"),
        builder_pairing_ids=("source_pairing_id", lambda x: "|".join(map(str, x))),
        builder_types=("pairing_type", lambda x: "|".join(sorted(set(map(str, x))))),
        builder_checks=("independent_check", lambda x: "|".join(sorted(set(map(str, x))))),
    )
    pricing_lookup = columns.assign(sequence=columns["flight_ids"].map(_sequence)).groupby(
        "sequence", dropna=False
    ).agg(
        pricing_count=("column_id", "size"),
        pricing_column_ids=("column_id", lambda x: "|".join(map(str, x))),
        pricing_types=("pairing_type", lambda x: "|".join(sorted(set(map(str, x))))),
    )
    result = pd.DataFrame({"sequence": all_sequences})
    result = result.join(builder_lookup, on="sequence").join(pricing_lookup, on="sequence")
    result["in_builder"] = result["builder_count"].fillna(0).gt(0)
    result["in_pricer"] = result["pricing_count"].fillna(0).gt(0)
    result["universe_difference"] = result.apply(
        lambda r: "BOTH" if r["in_builder"] and r["in_pricer"]
        else "BUILDER_ONLY" if r["in_builder"] else "PRICER_ONLY", axis=1
    )
    return result


def audit_duplicate_rows(path: Path) -> tuple[pd.DataFrame, dict[str, Any]]:
    frame = pd.read_csv(path)
    # Ignore exported row index columns; compare the actual source fields.
    compare = frame.loc[:, [c for c in frame.columns if not c.lower().startswith("unnamed:")]].copy()
    duplicate_mask = compare.duplicated(keep=False)
    duplicates = frame.loc[duplicate_mask].copy()
    return duplicates, {
        "rows_in_duplicate_report": int(len(frame)),
        "rows_in_exact_duplicate_groups": int(duplicate_mask.sum()),
        "exact_duplicate_groups": int(compare.loc[duplicate_mask].drop_duplicates().shape[0]),
        "comparison_columns": list(compare.columns),
        "note": "Exact row equality after ignoring exported Unnamed index columns; confirm against source before deleting records.",
    }


def audit_excluded_rows(path: Path) -> tuple[pd.DataFrame, dict[str, Any]]:
    frame = pd.read_csv(path)
    if "Pairing ID" not in frame.columns:
        raise ValueError(f"{path}: expected Pairing ID to classify unpaired report rows")
    out = frame.copy()
    out["exclusion_class"] = out["Pairing ID"].isna().map(
        {True: "BLANK_PAIRING_ID_EXCLUDED_BY_LOADER", False: "REVIEW"}
    )
    date_col = "Flight Date" if "Flight Date" in out.columns else None
    if date_col:
        dates = pd.to_datetime(out[date_col], errors="coerce")
        out["flight_date_parsed"] = dates.dt.strftime("%Y-%m-%d")
    return out, {
        "rows": int(len(out)),
        "blank_pairing_id_rows": int(out["Pairing ID"].isna().sum()),
        "distinct_flight_numbers": int(out["Flight Num"].nunique()) if "Flight Num" in out.columns else None,
        "equipment_counts": {str(k): int(v) for k, v in out["Eqp"].fillna("UNKNOWN").value_counts().sort_index().items()} if "Eqp" in out.columns else {},
        "date_min": str(pd.to_datetime(out[date_col], errors="coerce").min().date()) if date_col and pd.to_datetime(out[date_col], errors="coerce").notna().any() else None,
        "date_max": str(pd.to_datetime(out[date_col], errors="coerce").max().date()) if date_col and pd.to_datetime(out[date_col], errors="coerce").notna().any() else None,
        "note": "The current loader excludes blank Pairing ID rows; this audit does not establish whether each row should be operationally paired.",
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    coverage = _read_csv(Path(args.flight_coverage), {
        "flight_id", "lp_uncovered_slack", "candidate_columns_covering_flight"
    })
    columns = _read_csv(Path(args.column_audit), {"column_id", "pairing_type", "flight_ids"})
    builder = _flight_sequence_map(Path(args.workbook))

    triage = audit_uncovered(coverage, columns)
    sequences = audit_sequence_universes(builder, columns)
    duplicates, duplicate_summary = audit_duplicate_rows(Path(args.duplicate_rows))
    excluded, excluded_summary = audit_excluded_rows(Path(args.excluded_rows))

    triage.to_csv(output / "uncovered_flight_triage.csv", index=False)
    sequences.to_csv(output / "builder_pricer_sequence_diff.csv", index=False)
    duplicates.to_csv(output / "exact_duplicate_rows_review.csv", index=False)
    excluded.to_csv(output / "excluded_pairing_report_rows.csv", index=False)

    summary = {
        "scope": "Read-only recovery-gap audit; not an operational feasibility certificate.",
        "input_files": {
            "workbook": str(Path(args.workbook)),
            "flight_coverage": str(Path(args.flight_coverage)),
            "column_audit": str(Path(args.column_audit)),
            "duplicate_rows": str(Path(args.duplicate_rows)),
            "excluded_rows": str(Path(args.excluded_rows)),
        },
        "active_flights_in_coverage_audit": int(len(coverage)),
        "positive_uncovered_slack_flights": int(len(triage)),
        "positive_slack_without_pricing_candidate": int((triage["triage_class"] == "NO_PRICING_CANDIDATE").sum()),
        "positive_slack_with_candidate_not_selected": int((triage["triage_class"] == "CANDIDATE_EXISTS_NOT_SELECTED").sum()),
        "builder_pairing_rows": int(len(builder)),
        "unique_builder_sequences": int(builder["sequence"].nunique()),
        "pricing_column_rows": int(len(columns)),
        "unique_pricing_sequences": int(columns["flight_ids"].map(_sequence).nunique()),
        "exact_sequences_in_both": int((sequences["universe_difference"] == "BOTH").sum()),
        "builder_only_sequences": int((sequences["universe_difference"] == "BUILDER_ONLY").sum()),
        "pricer_only_sequences": int((sequences["universe_difference"] == "PRICER_ONLY").sum()),
        "duplicate_rows": duplicate_summary,
        "excluded_rows": excluded_summary,
        "outputs": [
            "uncovered_flight_triage.csv",
            "builder_pricer_sequence_diff.csv",
            "exact_duplicate_rows_review.csv",
            "excluded_pairing_report_rows.csv",
        ],
        "interpretation": {
            "no_pricing_candidate": "Candidate-generation or scope gap; not proof of legal infeasibility.",
            "candidate_exists_not_selected": "Inspect overlaps and master constraints before diagnosing.",
            "sequence_comparison": "Exact ordered flight-ID sequence only; pairing metadata may differ.",
        },
    }
    (output / "recovery_gap_audit.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workbook", default="output/pairing_experiment_1_week.xlsx")
    parser.add_argument("--flight-coverage", default="output/column_generation/1w_solution_audit/flight_coverage.csv")
    parser.add_argument("--column-audit", default="output/column_generation/1w_solution_audit/column_values_and_reduced_costs.csv")
    parser.add_argument("--duplicate-rows", default="output/audit/schedule_exact_duplicate_rows.csv")
    parser.add_argument("--excluded-rows", default="output/audit/unpaired_rows_excluded_from_pairing_report.csv")
    parser.add_argument("--output-dir", default="output/audit/recovery_gap")
    args = parser.parse_args()
    summary = run(args)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
