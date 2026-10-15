"""Cost-component helpers and legacy policy API; the canonical run objective lives in operational_objective.py."""
from __future__ import annotations
import csv, math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence
# Retained for backwards-compatible cost-component utilities only; not the active solver objective.\nOBJECTIVE_TERMS=("cancellation_penalty","retiming_penalty","crew_efficiency_penalty","crew_set_count")
DEFAULT_PRIORITY_ORDER=OBJECTIVE_TERMS
SOURCE_COST_KEYS={"cancellation_penalty":(),"retiming_penalty":("retime_base","retiming_cost_tier_1","retiming_cost_tier_2","retiming_cost_tier_3"),
 "crew_efficiency_penalty":("unutilized_hub_time_cost_scaling","unutilized_spoke_time_cost_scaling","fdp_wasted_cost_scaling"),
 "crew_set_count":("man_day_cost",)}
SOLVER_LAYER_REQUIREMENTS={
 "cancellation_penalty":{"decision_data":["cancelled_flight_decision"],"input_data":["validated_revenue_value_per_flight"],"blocker":"Uncovered slack is not a cancellation variable; revenue keys and fare units are unresolved."},
 "retiming_penalty":{"decision_data":["selected_flight_copy","offset_minutes"],"input_data":["source retiming tiers","approved retiming range"],"blocker":"The generic MIP does not select flight-copy offsets; cost tiers only define 0/5/10/15 minutes."},
 "crew_efficiency_penalty":{"decision_data":["selected_crew_duty","unused_hub_time_units","unused_spoke_time_units","wasted_fdp_units"],"input_data":["owner-confirmed time units and FDP definition"],"blocker":"The generic MIP exposes no duty-efficiency variables."},
 "crew_set_count":{"decision_data":["crew_set_assignment_variables","distinct_crew_set_count"],"input_data":["crew roster and crew-set definition"],"blocker":"Selected pairings are not distinct crew sets; no roster assignment layer is present."}}
class ObjectiveConfigurationError(ValueError): pass
@dataclass(frozen=True)
class ObjectiveConfig:
 mode:str="lexicographic"
 priority_order:tuple[str,...]=DEFAULT_PRIORITY_ORDER
 weights:Mapping[str,float]=field(default_factory=dict)
 priority_order_confirmed_by_owner:bool=False
 revenue_cancellation_enabled:bool=False
 def validate(self):
  validate_priority_order(self.priority_order)
  if self.mode not in {"lexicographic","weighted"}: raise ObjectiveConfigurationError("mode must be lexicographic or weighted")
  if self.mode=="weighted":
   missing=set(OBJECTIVE_TERMS)-set(self.weights);extra=set(self.weights)-set(OBJECTIVE_TERMS)
   if missing or extra: raise ObjectiveConfigurationError(f"Weighted mode needs exactly four explicit weights; missing={sorted(missing)}, extra={sorted(extra)}")
   for term,w in self.weights.items():
    if not math.isfinite(float(w)) or float(w)<0: raise ObjectiveConfigurationError(f"Invalid weight for {term}: {w!r}")
  elif self.weights: raise ObjectiveConfigurationError("Do not pass scalar weights in lexicographic mode")
  if self.revenue_cancellation_enabled and not self.priority_order_confirmed_by_owner:
   raise ObjectiveConfigurationError("Revenue cancellation term requires owner-confirmed priorities and revenue assumptions")
def validate_priority_order(order:Sequence[str])->tuple[str,...]:
 order=tuple(order)
 if len(order)!=4 or set(order)!=set(OBJECTIVE_TERMS): raise ObjectiveConfigurationError(f"priority_order must contain each term exactly once: {OBJECTIVE_TERMS}")
 return order
def load_cost_parameters(path:str|Path)->dict[str,float]:
 out={}
 with Path(path).open("r",encoding="utf-8-sig",newline="") as f:
  reader=csv.DictReader(f)
  if not reader.fieldnames or not {"parameter","value"}.issubset(reader.fieldnames): raise ValueError("Cost CSV requires parameter,value columns")
  for row in reader:
   name=(row.get("parameter") or "").strip();raw=(row.get("value") or "").strip()
   if not name or not raw: continue
   if name in out: raise ValueError(f"Duplicate cost parameter {name}")
   try: value=float(raw)
   except ValueError as e: raise ValueError(f"Non-numeric cost parameter {name}: {raw}") from e
   if not math.isfinite(value): raise ValueError(f"Non-finite cost parameter {name}")
   out[name]=value
 return out
def retiming_penalty_for_offset(offset_minutes:float,costs:Mapping[str,float])->float:
 offset=abs(float(offset_minutes))
 if not math.isfinite(offset): raise ValueError("offset_minutes must be finite")
 if offset==0:return float(costs.get("retime_base",0.0))
 if offset not in {5.0,10.0,15.0}:raise ValueError(f"No supplied retiming tier for {offset_minutes}; supported offsets are 0, 5, 10, 15 minutes")
 name=f"retiming_cost_tier_{int(offset//5)}"
 if name not in costs:raise KeyError(f"Missing source cost parameter {name}")
 return float(costs[name])
def retiming_penalty_from_offsets(offsets_minutes:Sequence[float],costs:Mapping[str,float])->float:
 return float(sum(retiming_penalty_for_offset(x,costs) for x in offsets_minutes))
def cancellation_penalty_from_validated_values(cancelled_flight_ids:Sequence[str],validated_value_by_flight:Mapping[str,float],*,revenue_enabled:bool)->float:
 if not revenue_enabled:
  if cancelled_flight_ids:raise ObjectiveConfigurationError("Revenue-derived cancellation penalties are disabled pending validation")
  return 0.0
 missing=sorted({str(x) for x in cancelled_flight_ids if str(x) not in validated_value_by_flight})
 if missing:raise ObjectiveConfigurationError(f"Missing validated cancellation values: {missing[:10]}")
 values=[]
 for fid in cancelled_flight_ids:
  value=float(validated_value_by_flight[str(fid)])
  if not math.isfinite(value) or value<0:raise ObjectiveConfigurationError(f"Invalid cancellation value for {fid}: {value}")
  values.append(value)
 return float(sum(values))
def crew_efficiency_penalty(*,unused_hub_time_units:float,unused_spoke_time_units:float,wasted_fdp_units:float,costs:Mapping[str,float],units_confirmed_by_owner:bool)->float:
 if not units_confirmed_by_owner:raise ObjectiveConfigurationError("Crew-efficiency units and metric definitions must be owner-confirmed")
 values=(unused_hub_time_units,unused_spoke_time_units,wasted_fdp_units)
 if any(not math.isfinite(float(x)) or float(x)<0 for x in values):raise ObjectiveConfigurationError("Efficiency input values must be finite and nonnegative")
 keys=("unutilized_hub_time_cost_scaling","unutilized_spoke_time_cost_scaling","fdp_wasted_cost_scaling")
 if any(k not in costs for k in keys):raise KeyError(f"Missing source scaling parameter(s): {[k for k in keys if k not in costs]}")
 return float(float(values[0])*costs[keys[0]]+float(values[1])*costs[keys[1]]+float(values[2])*costs[keys[2]])
def crew_set_count_component(distinct_crew_sets:int)->float:
 if isinstance(distinct_crew_sets,bool) or int(distinct_crew_sets)!=distinct_crew_sets or distinct_crew_sets<0:raise ObjectiveConfigurationError("distinct_crew_sets must be a nonnegative integer")
 return float(distinct_crew_sets)
def validate_component_values(components:Mapping[str,float])->dict[str,float]:
 missing=set(OBJECTIVE_TERMS)-set(components);extra=set(components)-set(OBJECTIVE_TERMS)
 if missing or extra:raise ObjectiveConfigurationError(f"Component ledger mismatch; missing={sorted(missing)}, extra={sorted(extra)}")
 result={}
 for term in OBJECTIVE_TERMS:
  value=float(components[term])
  if not math.isfinite(value) or value<0:raise ObjectiveConfigurationError(f"Component {term} must be finite and nonnegative")
  result[term]=value
 return result
def objective_key(components:Mapping[str,float],config:ObjectiveConfig|None=None):
 cfg=config or ObjectiveConfig();cfg.validate();values=validate_component_values(components)
 if not cfg.revenue_cancellation_enabled and values["cancellation_penalty"]!=0:raise ObjectiveConfigurationError("Nonzero cancellation penalty supplied while revenue term is disabled")
 return tuple(values[t] for t in cfg.priority_order) if cfg.mode=="lexicographic" else sum(values[t]*float(cfg.weights[t]) for t in OBJECTIVE_TERMS)
def objective_contract():
 """Return the canonical five-term run contract plus legacy cost-helper metadata.

 The four-term ObjectiveConfig interface is retained for compatibility with
 existing cost-component tests and experiments. It is not the objective used by
 the active joint recovery solver.
 """
 from crew_recovery.operational_objective import objective_contract as canonical_contract
 contract = canonical_contract(
  retiming_integrated=False,
  crew_efficiency_integrated=False,
  crew_roster_integrated=False,
  approved_operating_cost_integrated=False,
 )
 contract["legacy_cost_component_helpers"] = {
  "status": "COMPATIBILITY_API_NOT_USED_AS_ACTIVE_SOLVER_POLICY",
  "component_terms": list(OBJECTIVE_TERMS),
  "source_cost_parameters": SOURCE_COST_KEYS,
  "integration_requirements": SOLVER_LAYER_REQUIREMENTS,
  "note": "Use operational_terms and implemented_stages for the current run. The legacy ObjectiveConfig is not wired into joint_recovery.py.",
 }
 return contract
