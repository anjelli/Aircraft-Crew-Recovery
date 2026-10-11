"""Date- and frequency-aware adapter around the supplied MGT.py implementation.

The supplied MGT.py resolves rule specificity and arrival-time windows, but its resolver
does not consider the rule's Start Date, End Date, or Freq. This adapter filters the
rule table for each flight's local scheduled arrival date before delegating matching to
that unchanged implementation.

Frequency convention: digits 1..7 denote ISO weekdays (Monday=1, Sunday=7), matching
the independent validator already present in this project. Blank frequency is treated
as all days. Confirm these semantics with the airline before operational use.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
import re

import pandas as pd

_REQUIRED_RULE_COLUMNS = ("Start Date", "End Date", "Freq")


def _nonblank(values: pd.Series) -> pd.Series:
    return values.notna() & values.astype(str).str.strip().ne("")


def _parse_optional_dates(values: pd.Series, column: str) -> pd.Series:
    present = _nonblank(values)
    parsed = pd.to_datetime(values, errors="coerce")
    invalid = present & parsed.isna()
    if invalid.any():
        samples = values.loc[invalid].astype(str).head(5).tolist()
        raise ValueError(f"MGT column {column!r} has invalid date values: {samples}")
    return parsed.dt.normalize()


def _weekdays(value: object) -> set[int]:
    if pd.isna(value) or not str(value).strip():
        # A blank frequency means no day-of-week restriction.
        return set(range(1, 8))

    if isinstance(value, (int, float)) and float(value).is_integer():
        text = str(int(value))
    else:
        text = str(value).strip()
        if re.fullmatch(r"\d+\.0", text):
            text = text[:-2]

    if not text or any(ch not in "1234567" for ch in text):
        raise ValueError(
            f"Invalid MGT Freq value {value!r}; expected a sequence of weekday digits 1..7."
        )
    return {int(ch) for ch in text}


def filter_effective_mgt_rules(mgt_rules: pd.DataFrame, on_date: object) -> pd.DataFrame:
    """Return rules active on an arrival date, applying inclusive dates and weekday Freq.

    The input DataFrame is not mutated. Non-empty malformed date/frequency values fail
    loudly instead of being interpreted as active rules.
    """
    missing = [column for column in _REQUIRED_RULE_COLUMNS if column not in mgt_rules.columns]
    if missing:
        raise ValueError(f"MGT rules are missing required effective-date columns: {missing}")

    effective_date = pd.to_datetime(on_date, errors="coerce")
    if pd.isna(effective_date):
        raise ValueError(f"Invalid MGT effective date: {on_date!r}")
    effective_date = pd.Timestamp(effective_date).normalize()

    start = _parse_optional_dates(mgt_rules["Start Date"], "Start Date")
    end = _parse_optional_dates(mgt_rules["End Date"], "End Date")
    reversed_range = start.notna() & end.notna() & start.gt(end)
    if reversed_range.any():
        rows = mgt_rules.index[reversed_range].tolist()[:5]
        raise ValueError(f"MGT rules have Start Date later than End Date at rows {rows}")

    frequency_days = mgt_rules["Freq"].map(_weekdays)
    weekday = effective_date.isoweekday()
    active = (
        (start.isna() | start.le(effective_date))
        & (end.isna() | end.ge(effective_date))
        & frequency_days.map(lambda days: weekday in days)
    )
    return mgt_rules.loc[active].copy()


def apply_mgt_processing_by_arrival_date(
    df: pd.DataFrame,
    mgt_rules: pd.DataFrame,
    mgt_module,
    aircraft_groups,
    airport_to_country: dict,
    subtypes: list[str],
    *,
    arrival_date_col: str = "STA",
    sta_col: str = "STA",
):
    """Run the supplied MGT implementation separately for each local arrival date.

    Returns (processed_flights, cross_eqp_lookup_by_date, fallback_rows). The per-date
    lookup is deliberate: merging date-varying lookups into one (airport, equipment)
    dictionary would silently discard effective-date semantics.
    """
    if arrival_date_col not in df.columns:
        raise ValueError(f"Flight data is missing arrival date column {arrival_date_col!r}")
    arrival_dates = pd.to_datetime(df[arrival_date_col], errors="coerce")
    if arrival_dates.isna().any():
        bad_rows = df.index[arrival_dates.isna()].tolist()[:10]
        raise ValueError(
            f"Flights have missing/invalid local arrival dates in {arrival_date_col!r}; rows {bad_rows}"
        )

    dates = arrival_dates.dt.normalize()
    result_parts = []
    lookups_by_date = {}
    fallback_rows = []

    # Reset indices in each batch because the supplied MGT.py uses Series.loc[row.name].
    work = df.copy().reset_index(drop=True)
    work["__source_row_order"] = range(len(work))
    work["__effective_arrival_date"] = dates.reset_index(drop=True)

    for effective_date, group in work.groupby("__effective_arrival_date", sort=True):
        positions = group["__source_row_order"].tolist()
        flights = group.drop(columns=["__source_row_order", "__effective_arrival_date"]).copy()
        flights = flights.reset_index(drop=True)
        active_rules = filter_effective_mgt_rules(mgt_rules, effective_date)

        processed, cross_lookup, fallbacks = mgt_module.apply_mgt_processing(
            flights,
            active_rules.copy(),
            aircraft_groups,
            airport_to_country,
            subtypes,
            sta_col=sta_col,
        )
        processed["__source_row_order"] = positions
        result_parts.append(processed)
        date_key = pd.Timestamp(effective_date).date().isoformat()
        lookups_by_date[date_key] = cross_lookup
        fallback_rows.extend({"service_date": date_key, **row} for row in fallbacks)

    if not result_parts:
        # Preserve the shape for an empty but well-formed schedule.
        return work.drop(columns=["__source_row_order", "__effective_arrival_date"]), {}, []

    result = (
        pd.concat(result_parts, ignore_index=True)
        .sort_values("__source_row_order", kind="stable")
        .drop(columns="__source_row_order")
        .reset_index(drop=True)
    )
    return result, lookups_by_date, fallback_rows
