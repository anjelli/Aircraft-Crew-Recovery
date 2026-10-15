import pytest
from objective_spec import OBJECTIVE_TERMS,ObjectiveConfig,ObjectiveConfigurationError,objective_key,retiming_penalty_for_offset,retiming_penalty_from_offsets,cancellation_penalty_from_validated_values,crew_efficiency_penalty,crew_set_count_component,validate_component_values
COSTS={"retime_base":0.,"retiming_cost_tier_1":.001,"retiming_cost_tier_2":.003,"retiming_cost_tier_3":.006}
def test_lexicographic_priority_is_configurable():
 x={t:0. for t in OBJECTIVE_TERMS};x.update(retiming_penalty=.006,crew_efficiency_penalty=2.,crew_set_count=4.)
 cfg=ObjectiveConfig(priority_order=("retiming_penalty","cancellation_penalty","crew_set_count","crew_efficiency_penalty"))
 assert objective_key(x,cfg)==(.006,0.,4.,2.)
def test_weighted_mode_requires_four_explicit_weights():
 with pytest.raises(ObjectiveConfigurationError,match="exactly four explicit weights"):ObjectiveConfig(mode="weighted",weights={"cancellation_penalty":1.}).validate()
def test_revenue_gate_and_validated_values():
 with pytest.raises(ObjectiveConfigurationError,match="owner-confirmed"):ObjectiveConfig(revenue_cancellation_enabled=True).validate()
 with pytest.raises(ObjectiveConfigurationError,match="disabled"):cancellation_penalty_from_validated_values(["f1"],{"f1":10},revenue_enabled=False)
 assert cancellation_penalty_from_validated_values(["f1","f2"],{"f1":10,"f2":3},revenue_enabled=True)==13
 with pytest.raises(ObjectiveConfigurationError,match="Missing validated"):cancellation_penalty_from_validated_values(["f2"],{},revenue_enabled=True)
def test_nonzero_cancellation_disallowed_when_gate_off():
 x={t:0. for t in OBJECTIVE_TERMS};x["cancellation_penalty"]=10
 with pytest.raises(ObjectiveConfigurationError,match="disabled"):objective_key(x)
def test_retiming_cost_tiers_do_not_interpolate():
 assert [retiming_penalty_for_offset(x,COSTS) for x in (0,5,-10,15)]==[0.,.001,.003,.006]
 assert retiming_penalty_from_offsets([0,5,-10],COSTS)==.004
 with pytest.raises(ValueError,match="No supplied"):retiming_penalty_for_offset(7,COSTS)
def test_crew_efficiency_requires_owner_confirmed_units():
 costs={"unutilized_hub_time_cost_scaling":20.,"unutilized_spoke_time_cost_scaling":40.,"fdp_wasted_cost_scaling":1.}
 with pytest.raises(ObjectiveConfigurationError,match="owner-confirmed"):crew_efficiency_penalty(unused_hub_time_units=1,unused_spoke_time_units=2,wasted_fdp_units=3,costs=costs,units_confirmed_by_owner=False)
 assert crew_efficiency_penalty(unused_hub_time_units=1,unused_spoke_time_units=2,wasted_fdp_units=3,costs=costs,units_confirmed_by_owner=True)==103.
 assert crew_set_count_component(5)==5.
 with pytest.raises(ObjectiveConfigurationError,match="nonnegative integer"):crew_set_count_component(1.2)
def test_component_ledger_validation():
 x={t:0. for t in OBJECTIVE_TERMS};assert validate_component_values(x)==x;x["crew_set_count"]=-1
 with pytest.raises(ObjectiveConfigurationError,match="finite and nonnegative"):validate_component_values(x)



def test_legacy_objective_spec_contract_points_to_canonical_run_policy():
    from objective_spec import objective_contract
    contract = objective_contract()
    assert contract["priority_order"] == [
        "cancellation_count",
        "total_absolute_retiming_minutes",
        "approved_operating_cost",
        "crew_efficiency_cost",
        "distinct_crew_set_count",
    ]
    assert contract["legacy_cost_component_helpers"]["status"] == (
        "COMPATIBILITY_API_NOT_USED_AS_ACTIVE_SOLVER_POLICY"
    )
