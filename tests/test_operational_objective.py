import pytest

from crew_recovery.operational_objective import ObjectiveComponents, objective_contract, objective_key


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
