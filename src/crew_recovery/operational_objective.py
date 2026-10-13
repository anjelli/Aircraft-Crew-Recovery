"""Lexicographic operational objective contract shared by recovery experiments."""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Mapping

OBJECTIVE_ORDER = (
    "cancellation_count",
    "total_absolute_retiming_minutes",
    "approved_operating_cost",
    "crew_efficiency_cost",
    "distinct_crew_set_count",
)


@dataclass(frozen=True)
class ObjectiveComponents:
    cancellation_count: int
    total_absolute_retiming_minutes: float
    approved_operating_cost: float
    crew_efficiency_cost: float = 0.0
    distinct_crew_set_count: int = 0

    def as_mapping(self) -> dict[str, float]:
        if isinstance(self.cancellation_count, bool) or self.cancellation_count < 0:
            raise ValueError("cancellation_count must be a nonnegative integer")
        if int(self.cancellation_count) != self.cancellation_count:
            raise ValueError("cancellation_count must be an integer")
        if isinstance(self.distinct_crew_set_count, bool) or self.distinct_crew_set_count < 0:
            raise ValueError("distinct_crew_set_count must be a nonnegative integer")
        if int(self.distinct_crew_set_count) != self.distinct_crew_set_count:
            raise ValueError("distinct_crew_set_count must be an integer")
        values = {
            "cancellation_count": float(self.cancellation_count),
            "total_absolute_retiming_minutes": float(self.total_absolute_retiming_minutes),
            "approved_operating_cost": float(self.approved_operating_cost),
            "crew_efficiency_cost": float(self.crew_efficiency_cost),
            "distinct_crew_set_count": float(self.distinct_crew_set_count),
        }
        for key, value in values.items():
            if not isfinite(value) or value < 0:
                raise ValueError(f"{key} must be finite and nonnegative")
        return values


def objective_key(components: ObjectiveComponents) -> tuple[float, ...]:
    """Compare solutions lexicographically; no guessed big-M weights."""
    values = components.as_mapping()
    return tuple(values[name] for name in OBJECTIVE_ORDER)


def objective_contract(*, retiming_integrated: bool, crew_roster_integrated: bool) -> dict:
    """Describe which lexicographic terms the current solver actually supports."""
    return {
        "mode": "lexicographic",
        "priority_order": list(OBJECTIVE_ORDER),
        "retiming_term_active": bool(retiming_integrated),
        "crew_efficiency_term_active": False,
        "distinct_crew_set_term_active": bool(crew_roster_integrated),
        "note": (
            "Inactive terms must be reported as not integrated, not treated as proven zero. "
            "The active operate/cancel prototype minimizes cancellations then its supplied "
            "pairing/aircraft cost objective."
        ),
    }
