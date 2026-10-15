import pytest

from crew_recovery.operational_objective import ObjectiveComponents, objective_contract, objective_key, current_recovery_objective_ledger


def test_objective_is_lexicographic_cancellation_first():
    fewer_cancellations = ObjectiveComponents(0, 15, 1_000_000, 100, 50)
    more_cancellations = ObjectiveComponents(1, 0, 0, 0, 0)
    assert objective_key(fewer_cancellations) < objective_key(more_cancellations)


def test_objective_compares_retiming_before_operating_cost():
    less_retiming = ObjectiveComponents(0, 5, 5000, 0, 0)
    more_retiming = ObjectiveComponents(0, 10, 1, 0, 0)
    assert objective_key(less_retiming) < objective_key(more_retiming)


def test_inactive_objective_terms_are_not_claimed_as_integrated():
    contract = objective_contract(retiming_integrated=False, crew_roster_integrated=False)
    assert contract["retiming_term_active"] is False
    assert contract["distinct_crew_set_term_active"] is False


def test_objective_rejects_negative_components():
    with pytest.raises(ValueError):
        objective_key(ObjectiveComponents(0, -5, 10))



def test_contract_distinguishes_active_partial_and_unintegrated_terms():
    contract = objective_contract()
    assert contract["policy_status"] == "PROPOSED_PENDING_OWNER_APPROVAL"
    status_by_term = {term["name"]: term["implementation_status"] for term in contract["terms"]}
    assert status_by_term["cancellation_count"] == "active"
    assert status_by_term["approved_operating_cost"] == "partial_proxy_selected_pairing_cost_plus_aircraft_start_cost"
    assert status_by_term["total_absolute_retiming_minutes"] == "not_integrated"
    assert contract["inactive_terms_are_not_zero"] is True


def test_current_objective_ledger_reports_null_for_unintegrated_terms():
    ledger = current_recovery_objective_ledger(
        cancellation_count=3,
        selected_pairing_cost=1250.0,
        aircraft_start_cost=50.0,
        solver_status="FEASIBLE_NOT_PROVEN_OPTIMAL",
        solver_reported_secondary_objective=1300.0,
    )
    assert ledger["implemented_stages"][0]["value"] == 3
    assert ledger["implemented_stages"][1]["value"] == 1300.0
    assert ledger["implemented_stages"][1]["values_reconcile"] is True
    assert ledger["implemented_stages"][0]["status"] == "FEASIBLE_NOT_PROVEN_OPTIMAL"
    assert ledger["implemented_stages"][1]["status"] == "FEASIBLE_NOT_PROVEN_OPTIMAL"
    terms = {term["name"]: term for term in ledger["operational_terms"]}
    assert terms["approved_operating_cost"]["status"] == "partial_proxy"
    assert terms["total_absolute_retiming_minutes"]["value"] is None
    assert terms["crew_efficiency_cost"]["value"] is None
    assert terms["distinct_crew_set_count"]["value"] is None


def test_current_objective_ledger_rejects_invalid_cost_values():
    with pytest.raises(ValueError, match="finite and nonnegative"):
        current_recovery_objective_ledger(
            cancellation_count=0,
            selected_pairing_cost=float("nan"),
            aircraft_start_cost=0.0,
            solver_status="test",
        )



def test_objective_ledger_only_claims_optimality_when_the_solver_proves_it():
    ledger = current_recovery_objective_ledger(
        cancellation_count=1,
        selected_pairing_cost=100.0,
        aircraft_start_cost=0.0,
        solver_status="PROTOTYPE_OPTIMAL_OVER_SUPPLIED_COLUMNS",
        solver_reported_secondary_objective=100.0,
        minimum_cancellation_count_proven=True,
        secondary_objective_proven_optimal=True,
    )
    assert ledger["implemented_stages"][0]["status"] == "OPTIMAL_WITHIN_RESTRICTED_MODEL"
    assert ledger["implemented_stages"][1]["status"] == "OPTIMAL_WITHIN_RESTRICTED_MODEL"
