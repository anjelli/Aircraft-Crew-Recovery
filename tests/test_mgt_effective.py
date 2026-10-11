from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from mgt_effective import (
    apply_mgt_processing_by_arrival_date,
    filter_effective_mgt_rules,
)
from project_paths import INPUT_DIR, ORIGINALS_DIR, input_file, original_file


ROOT = Path(__file__).resolve().parents[1]


def _rules(rows):
    return pd.DataFrame(
        rows,
        columns=["Start Date", "End Date", "Freq", "MGT"],
    )


def test_start_and_end_dates_are_inclusive_and_expired_rules_are_excluded():
    rules = _rules([
        ["2026-10-25", "2026-10-25", "7", 50],
        ["2026-10-26", "2026-10-27", "1", 60],
    ])
    assert filter_effective_mgt_rules(rules, "2026-10-25")["MGT"].tolist() == [50]
    assert filter_effective_mgt_rules(rules, "2026-10-26")["MGT"].tolist() == [60]
    assert filter_effective_mgt_rules(rules, "2026-10-27").empty


def test_frequency_uses_iso_weekday_digits_and_blank_means_daily():
    rules = _rules([
        ["2026-10-25", "2026-10-31", "7", 50],  # Sunday
        ["2026-10-25", "2026-10-31", "1", 60],  # Monday
        ["2026-10-25", "2026-10-31", None, 70],
    ])
    assert filter_effective_mgt_rules(rules, "2026-10-25")["MGT"].tolist() == [50, 70]
    assert filter_effective_mgt_rules(rules, "2026-10-26")["MGT"].tolist() == [60, 70]


def test_malformed_rule_metadata_fails_closed():
    bad_date = _rules([["not-a-date", "2026-10-31", "1234567", 50]])
    with pytest.raises(ValueError, match="Start Date"):
        filter_effective_mgt_rules(bad_date, "2026-10-25")

    bad_frequency = _rules([["2026-10-25", "2026-10-31", "MON-FRI", 50]])
    with pytest.raises(ValueError, match="Freq"):
        filter_effective_mgt_rules(bad_frequency, "2026-10-26")


def test_repository_mgt_workbook_does_not_apply_rule_after_end_date():
    rules = pd.read_excel(original_file("MGT.xlsx"))
    assert not filter_effective_mgt_rules(rules, "2027-03-27").empty
    assert filter_effective_mgt_rules(rules, "2027-03-28").empty


def test_schedule_mgt_is_resolved_by_local_arrival_date_and_preserves_row_order():
    flights = pd.DataFrame({
        "Flight": ["sunday", "monday", "sunday_again"],
        "STA": pd.to_datetime([
            "2026-10-25 23:00",
            "2026-10-26 02:00",
            "2026-10-25 12:00",
        ]),
    })
    rules = _rules([
        ["2026-10-25", "2026-10-31", "7", 50],
        ["2026-10-25", "2026-10-31", "1", 60],
    ])

    class FakeMGTModule:
        @staticmethod
        def apply_mgt_processing(df, filtered_rules, *args, **kwargs):
            result = df.copy()
            result["MGT"] = (
                filtered_rules.iloc[0]["MGT"] if not filtered_rules.empty else float("nan")
            )
            return result, {"active_rules": len(filtered_rules)}, []

    processed, lookup_by_date, fallbacks = apply_mgt_processing_by_arrival_date(
        flights,
        rules,
        FakeMGTModule,
        {},
        {},
        [],
        arrival_date_col="STA",
        sta_col="STA",
    )
    assert processed["Flight"].tolist() == ["sunday", "monday", "sunday_again"]
    assert processed["MGT"].tolist() == [50, 60, 50]
    assert set(lookup_by_date) == {"2026-10-25", "2026-10-26"}
    assert fallbacks == []


def test_no_effective_rule_is_not_replaced_by_an_expired_rule():
    flights = pd.DataFrame({"Flight": ["late"], "STA": pd.to_datetime(["2027-03-28 01:00"])})
    rules = _rules([["2026-10-25", "2027-03-27", "1234567", 50]])

    class FakeMGTModule:
        @staticmethod
        def apply_mgt_processing(df, filtered_rules, *args, **kwargs):
            result = df.copy()
            result["MGT"] = float("nan") if filtered_rules.empty else filtered_rules.iloc[0]["MGT"]
            return result, {}, []

    processed, _, _ = apply_mgt_processing_by_arrival_date(
        flights, rules, FakeMGTModule, {}, {}, [], arrival_date_col="STA", sta_col="STA"
    )
    assert pd.isna(processed.loc[0, "MGT"])


def test_input_and_source_paths_do_not_fall_back_to_project_root():
    assert original_file("MGT.xlsx") == ORIGINALS_DIR / "MGT.xlsx"
    assert input_file("schedule.xlsx") == INPUT_DIR / "schedule.xlsx"
    # The repository has a root-level MGT.xlsx copy, but runtime source configuration
    # must come from originals/; accidental root/input shadowing is not accepted.
    with pytest.raises(FileNotFoundError, match="prepared input"):
        input_file("MGT.xlsx")


def test_full_horizon_attach_mgt_fails_closed_for_arrivals_after_rule_expiry():
    import importlib.util

    schedule = pd.read_excel(input_file("schedule.xlsx"))
    for column in ("Day", "STD", "STA", "STD_sin", "STA_sin"):
        schedule[column] = pd.to_datetime(schedule[column], errors="raise")
    schedule["Day"] = schedule["Day"].dt.normalize()
    schedule["Eqp"] = schedule["Eqp"].astype(str).str.strip()

    # MGT is applied using the scheduled local arrival date, not the departure Day label.
    late_arrivals = schedule.loc[
        schedule["STA"].dt.normalize().ge(pd.Timestamp("2027-03-28"))
    ].copy().reset_index(drop=True)
    assert not late_arrivals.empty, "Expected source schedule arrivals after MGT expiry"

    airport_to_country = {}
    for airport, group in schedule.groupby("To"):
        values = {str(v).strip().upper() for v in group["Arvl Ctry"].dropna() if str(v).strip()}
        if len(values) == 1:
            airport_to_country[str(airport).strip().upper()] = next(iter(values))
    for airport, group in schedule.groupby("From"):
        values = {str(v).strip().upper() for v in group["Dept Ctry"].dropna() if str(v).strip()}
        if len(values) == 1:
            airport_to_country.setdefault(str(airport).strip().upper(), next(iter(values)))

    source_spec = importlib.util.spec_from_file_location("mgt_source_for_test", original_file("MGT.py"))
    assert source_spec and source_spec.loader
    source_mgt = importlib.util.module_from_spec(source_spec)
    source_spec.loader.exec_module(source_mgt)

    runner_spec = importlib.util.spec_from_file_location(
        "full_horizon_experiment_for_test", ROOT / "run_pairing_experiment_166_days.py"
    )
    assert runner_spec and runner_spec.loader
    runner = importlib.util.module_from_spec(runner_spec)
    runner_spec.loader.exec_module(runner)

    with pytest.raises(RuntimeError, match="Expired rules are not reused"):
        runner.attach_mgt(
            late_arrivals,
            source_mgt,
            original_file("MGT.xlsx"),
            airport_to_country,
            sorted(schedule["Eqp"].astype(str).str.upper().unique()),
        )
