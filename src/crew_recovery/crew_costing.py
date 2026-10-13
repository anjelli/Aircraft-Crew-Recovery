"""Transparent baseline crew salary costing for pairing candidates.

The current source cost table defines a man-day amount, and the project notes
state that a duty spanning midnight incurs salary on both calendar days. This
module implements that rule as a provisional salary-only component. It does not
claim to reproduce every allowance or the source report's Total Cost formula.
"""
from __future__ import annotations

from datetime import timedelta
from math import isfinite
from typing import Mapping, Sequence


def _local_datetime(flight: Mapping[str, object], key: str):
    value = flight.get(key)
    if value is None or not hasattr(value, "date"):
        raise ValueError(f"Flight {flight.get('flight_id')!r} is missing datetime {key}")
    return value


def calendar_salary_days(
    sequence: Sequence[Mapping[str, object]],
    duty_splits: Sequence[Sequence[int]],
    *,
    duty_start_buffer_minutes: int = 60,
    duty_end_buffer_minutes: int = 30,
) -> int:
    """Count paid calendar days per duty, including a second day across midnight."""
    if not sequence or not duty_splits:
        raise ValueError("sequence and duty_splits must be non-empty")
    if duty_start_buffer_minutes < 0 or duty_end_buffer_minutes < 0:
        raise ValueError("duty buffers must be nonnegative")
    covered = [idx for split in duty_splits for idx in split]
    if sorted(covered) != list(range(len(sequence))):
        raise ValueError("duty_splits must partition every flight index exactly once")
    total = 0
    for split in duty_splits:
        if not split:
            raise ValueError("empty duty is not allowed")
        start = _local_datetime(sequence[min(split)], "STD_local") - timedelta(minutes=duty_start_buffer_minutes)
        end = _local_datetime(sequence[max(split)], "STA_local") + timedelta(minutes=duty_end_buffer_minutes)
        if end < start:
            raise ValueError("duty end precedes duty start")
        total += (end.date() - start.date()).days + 1
    return total


def crew_salary_cost(
    sequence: Sequence[Mapping[str, object]],
    duty_splits: Sequence[Sequence[int]],
    *,
    man_day_cost: float,
    duty_start_buffer_minutes: int = 60,
    duty_end_buffer_minutes: int = 30,
) -> float:
    """Salary-only cost; crossing midnight charges each calendar day touched."""
    rate = float(man_day_cost)
    if not isfinite(rate) or rate < 0:
        raise ValueError("man_day_cost must be finite and nonnegative")
    days = calendar_salary_days(
        sequence,
        duty_splits,
        duty_start_buffer_minutes=duty_start_buffer_minutes,
        duty_end_buffer_minutes=duty_end_buffer_minutes,
    )
    return rate * days


def generated_pairing_cost_callback(
    *,
    man_day_cost: float,
    duty_start_buffer_minutes: int = 60,
    duty_end_buffer_minutes: int = 30,
):
    """Return the callback expected by ResourceConstrainedPairingPricer."""
    def cost(sequence, splits, pairing_type):
        return crew_salary_cost(
            sequence,
            splits,
            man_day_cost=man_day_cost,
            duty_start_buffer_minutes=duty_start_buffer_minutes,
            duty_end_buffer_minutes=duty_end_buffer_minutes,
        )
    return cost
