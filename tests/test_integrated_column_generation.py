from __future__ import annotations

import pytest

from column_generation import PairingColumn
from crew_recovery.integrated_column_generation import PricingBatch, solve_with_pricing


def test_integrated_master_calls_dynamic_pricing_until_certified():
    hidden_column = PairingColumn("generated-on-demand", ("F1", "F2"), cost=1.0)
    calls = []

    def oracle(duals, active_ids, tolerance, batch_size):
        calls.append((dict(duals), active_ids, tolerance, batch_size))
        if hidden_column.column_id not in active_ids:
            return PricingBatch(
                columns=(hidden_column,),
                best_reduced_cost=1.0 - duals["F1"] - duals["F2"],
                certified_no_improving_column=False,
                explored_states=7,
                status="exact-pricing",
            )
        return PricingBatch(
            columns=(),
            best_reduced_cost=0.0,
            certified_no_improving_column=True,
            explored_states=9,
            status="exact-pricing",
        )

    result = solve_with_pricing(["F1", "F2"], oracle)

    assert result.converged
    assert result.stop_reason == "pricing_certified_no_improving_column"
    assert len(calls) == 2
    assert [c.column_id for c in result.columns] == ["generated-on-demand"]
    assert result.solution.objective == pytest.approx(1.0)
    assert result.history[0]["columns_added"] == 1
    assert result.history[1]["certified"] == 1


def test_empty_heuristic_pricing_does_not_claim_convergence():
    def oracle(duals, active_ids, tolerance, batch_size):
        return PricingBatch(
            columns=(),
            best_reduced_cost=None,
            certified_no_improving_column=False,
            explored_states=3,
            status="heuristic-time-limit",
        )

    result = solve_with_pricing(["F1"], oracle)

    assert not result.converged
    assert result.stop_reason == "pricing_inconclusive_no_column_returned"
    assert result.pricing_status == "heuristic-time-limit"


def test_pricing_oracle_cannot_return_non_improving_column():
    def oracle(duals, active_ids, tolerance, batch_size):
        # A single-flight column costs 1 and the dual is at least 2 initially,
        # so this one would be improving; report a deliberately invalid cost.
        return PricingBatch(
            columns=(PairingColumn("bad", ("F1",), cost=100.0),),
            best_reduced_cost=-1.0,
            certified_no_improving_column=False,
        )

    with pytest.raises(ValueError, match="non-improving column"):
        solve_with_pricing(["F1"], oracle)


def test_pricing_oracle_cannot_cover_unknown_flight():
    def oracle(duals, active_ids, tolerance, batch_size):
        return PricingBatch(
            columns=(PairingColumn("bad", ("NOT_ACTIVE",), cost=0.0),),
            best_reduced_cost=-1.0,
            certified_no_improving_column=False,
        )

    with pytest.raises(ValueError, match="unknown/inactive flights"):
        solve_with_pricing(["F1"], oracle)


def test_duplicate_column_ids_from_oracle_are_rejected():
    def oracle(duals, active_ids, tolerance, batch_size):
        return PricingBatch(
            columns=(
                PairingColumn("same", ("F1",), cost=0.0),
                PairingColumn("same", ("F1",), cost=0.0),
            ),
            best_reduced_cost=-2.0,
            certified_no_improving_column=False,
        )

    with pytest.raises(ValueError, match="duplicate column ID"):
        solve_with_pricing(["F1"], oracle)
