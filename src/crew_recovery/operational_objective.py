"""Canonical objective contract and objective ledger for recovery experiments.

The priority order is a proposed operational policy, not owner-approved yet. The
ledger distinguishes the objective the current prototype actually optimized from
operational terms whose decisions/costs are not yet integrated.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isclose, isfinite
from typing import Mapping

OBJECTIVE_ORDER = (
    "cancellation_count",
    "total_absolute_retiming_minutes",
    "approved_operating_cost",
    "crew_efficiency_cost",
    "distinct_crew_set_count",
)

_OBJECTIVE_LABELS = {
    "cancellation_count": "Minimize cancelled eligible flights",
    "total_absolute_retiming_minutes": "Minimize approved absolute retiming",
    "approved_operating_cost": "Minimize validated incremental operating cost",
    "crew_efficiency_cost": "Minimize validated unused-duty and wasted-FDP cost",
    "distinct_crew_set_count": "Minimize distinct assigned crew sets",
}


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
    """Compare component ledgers lexicographically; no guessed big-M weights."""
    values = components.as_mapping()
    return tuple(values[name] for name in OBJECTIVE_ORDER)


def objective_contract(
    *,
    retiming_integrated: bool = False,
    crew_efficiency_integrated: bool = False,
    crew_roster_integrated: bool = False,
    approved_operating_cost_integrated: bool = False,
) -> dict:
    """Describe the proposed priority order and current implementation boundary.

    Current recovery code optimizes cancellation count first and then a partial
    secondary cost (selected pairing cost plus aircraft-start cost). The broader
    five-term operational policy is not fully integrated or owner-approved.
    """
    statuses = {
        "cancellation_count": "active",
        "total_absolute_retiming_minutes": (
            "integrated" if retiming_integrated else "not_integrated"
        ),
        "approved_operating_cost": (
            "integrated"
            if approved_operating_cost_integrated
            else "partial_proxy_selected_pairing_cost_plus_aircraft_start_cost"
        ),
        "crew_efficiency_cost": (
            "integrated" if crew_efficiency_integrated else "not_integrated"
        ),
        "distinct_crew_set_count": (
            "integrated" if crew_roster_integrated else "not_integrated"
        ),
    }
    return {
        "schema_version": 1,
        "policy": "lexicographic",
        "policy_status": "PROPOSED_PENDING_OWNER_APPROVAL",
        "priority_order": list(OBJECTIVE_ORDER),
        # Preserve pre-ledger keys for existing callers while making the richer
        # per-term status list the canonical interface.
        "retiming_term_active": bool(retiming_integrated),
        "crew_efficiency_term_active": bool(crew_efficiency_integrated),
        "distinct_crew_set_term_active": bool(crew_roster_integrated),
        "objective_labels": dict(_OBJECTIVE_LABELS),
        "terms": [
            {
                "name": term,
                "priority": priority,
                "label": _OBJECTIVE_LABELS[term],
                "implementation_status": statuses[term],
            }
            for priority, term in enumerate(OBJECTIVE_ORDER, start=1)
        ],
        "current_solver_objective": {
            "stage_1": "minimize cancellation_count",
            "stage_2": (
                "fix stage-1 cancellation count, then minimize selected pairing "
                "cost plus aircraft-start cost"
            ),
            "secondary_cost_scope": (
                "Partial cost proxy only; not the complete owner-approved operating "
                "cost, and not comparable to unconfirmed retiming/efficiency scalers."
            ),
        },
        "inactive_terms_are_not_zero": True,
        "notes": [
            "The current joint solver supports operate/cancel only; flight-copy retiming is not integrated.",
            "Selected pairings are not distinct crew sets; individual crew roster assignment is not integrated.",
            "The objective priority order requires business-owner confirmation.",
            "A solver optimum is limited to the candidate columns and aircraft route arcs supplied to that run.",
        ],
    }


def current_recovery_objective_ledger(
    *,
    cancellation_count: int,
    selected_pairing_cost: float,
    aircraft_start_cost: float,
    solver_status: str,
    solver_reported_secondary_objective: float | None = None,
) -> dict:
    """Return run-specific values for active stages and explicit nulls for blocked terms."""
    if isinstance(cancellation_count, bool) or int(cancellation_count) != cancellation_count or cancellation_count < 0:
        raise ValueError("cancellation_count must be a nonnegative integer")
    costs = {
        "selected_pairing_cost": float(selected_pairing_cost),
        "aircraft_start_cost": float(aircraft_start_cost),
    }
    if any(not isfinite(value) or value < 0 for value in costs.values()):
        raise ValueError("Objective cost components must be finite and nonnegative")

    recomputed_secondary = sum(costs.values())
    reported_secondary = (
        None if solver_reported_secondary_objective is None
        else float(solver_reported_secondary_objective)
    )
    if reported_secondary is not None and (
        not isfinite(reported_secondary) or reported_secondary < 0
    ):
        raise ValueError("solver_reported_secondary_objective must be finite and nonnegative")

    return {
        "status": "SOLVED",
        "solver_status": str(solver_status),
        "contract": objective_contract(),
        "implemented_stages": [
            {
                "stage": 1,
                "criterion": "cancellation_count",
                "sense": "minimize",
                "value": int(cancellation_count),
                "status": "optimized",
            },
            {
                "stage": 2,
                "criterion": "selected_pairing_cost_plus_aircraft_start_cost",
                "sense": "minimize_after_fixing_stage_1",
                "value": recomputed_secondary,
                "components": costs,
                "solver_reported_value": reported_secondary,
                "values_reconcile": (
                    None if reported_secondary is None
                    else isclose(recomputed_secondary, reported_secondary, rel_tol=1e-7, abs_tol=1e-6)
                ),
                "status": "optimized_or_best_available_within_solver_status",
                "scope": "partial cost proxy; not the full approved operating-cost objective",
            },
        ],
        "operational_terms": [
            {
                "name": "cancellation_count",
                "status": "active",
                "value": int(cancellation_count),
            },
            {
                "name": "total_absolute_retiming_minutes",
                "status": "not_integrated",
                "value": None,
            },
            {
                "name": "approved_operating_cost",
                "status": "partial_proxy",
                "value": recomputed_secondary,
                "components": costs,
            },
            {
                "name": "crew_efficiency_cost",
                "status": "not_integrated",
                "value": None,
            },
            {
                "name": "distinct_crew_set_count",
                "status": "not_integrated",
                "value": None,
            },
        ],
        "warning": (
            "Null means the term is not calculated by this solver. It must not be "
            "reported as zero or interpreted as an operationally optimized value."
        ),
    }
