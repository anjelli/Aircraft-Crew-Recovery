"""Locate production solver and decompose saved coverage-only MIP baselines."""
from __future__ import annotations
import argparse,json,re,sys
from datetime import date
from pathlib import Path
import pandas as pd
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
for _project_path in (PROJECT_ROOT, SRC_DIR):
    if str(_project_path) not in sys.path:
        sys.path.insert(0, str(_project_path))

from crew_recovery.objective_spec import objective_contract,load_cost_parameters
ROOT=PROJECT_ROOT
CANDIDATES=("mip.py","mip_solver.py","operational_solver.py","column_generation.py","branch_and_price.py","aircraft_timeline_solver.py","solver/column_generation.py","solver/aircraft_timeline.py","run_end_to_end.py")
LOGS=(("1W","logs/mip_1W.log"),("FULL","logs/mip_155D.log"))
def parse_embedded_summary(text):
 m=re.search(r"(?m)^\s*\{",text)
 if not m:raise ValueError("No JSON summary in saved MIP log")
 result,_=json.JSONDecoder().raw_decode(text[text.index("{",m.start()):])
 if not isinstance(result,dict):raise ValueError("Expected a JSON summary object")
 return result
def calendar_days(start,end):return (date.fromisoformat(end)-date.fromisoformat(start)).days+1
def baseline_breakdown(root):
 rows=[];summaries={}
 for tag,rel in LOGS:
  p=root/rel
  if not p.is_file():rows.append({"Horizon Tag":tag,"Source Log":rel,"Status":"MISSING_SAVED_BASELINE"});continue
  text=p.read_text(encoding="utf-8",errors="replace");s=parse_embedded_summary(text);summaries[tag]=s
  start=str(s["Start Date"]);end=str(s.get("End Date Used",s.get("End Date")))
  active=int(s["Active Flights"]);selected=int(s["Selected Pairings"]);uncovered=int(s["Flights Uncovered After MIP"])
  factor=active+1;slack=factor*uncovered;pairing_cost=selected;total=slack+pairing_cost;reported=float(s["Objective"])
  pat=r"\[1W\].*?runtime=([0-9.]+)s" if tag=="1W" else r"\[166D\].*?runtime=([0-9.]+)s"
  m=re.search(pat,text);runtime=float(m.group(1)) if m else None
  covered=int(s["Flights Covered After MIP"])
  rows.append({"Horizon Tag":tag,"True Calendar Days Inclusive":calendar_days(start,end),"Start Date":start,"End Date":end,
   "Active Flights":active,"Covered Flights":covered,"Coverage %":round(100*covered/max(1,active),3),"Selected Pairings":selected,
   "Uncovered Flights":uncovered,"M Factor (active flights + 1)":factor,"Uncovered Slack Objective Component":slack,
   "Selected Pairing Count Objective Component":pairing_cost,"Recomputed Coverage-Only Objective":total,"Reported Coverage-Only Objective":reported,
   "Objective Decomposition Matches":abs(total-reported)<1e-7,"MIP Runtime Seconds (saved log)":runtime,"Saved Solver Status":s.get("Solver Status"),
   "Operational Four-Term Objective Run":"NOT RUN — production solver absent","Source Log":rel,
   "Notes":"Historical saved artifact, not rerun." + (" Legacy label says 166 days; inclusive source span is 155." if tag=="FULL" else "")})
 return pd.DataFrame(rows),summaries
def readiness_rows(costs):
 data=[
 ("cancellation_penalty",1,"No usable cancellation parameter; needs validated per-flight revenue value","None; uncovered slack is not cancellation","No cancellation variable; revenue keys and fare units unresolved."),
 ("retiming_penalty",2,"retime_base; retiming_cost_tier_1/2/3","None; MIP does not choose flight-copy offsets","Needs selected flight-copy and offset variables; approved range required."),
 ("crew_efficiency_penalty",3,"unutilized_hub_time_cost_scaling; unutilized_spoke_time_cost_scaling; fdp_wasted_cost_scaling","None; no duty-efficiency variables","Needs unused-time/FDP metrics and owner-confirmed units."),
 ("crew_set_count",4,"man_day_cost is available but not assumed equivalent to cost per set","None; pairing count is not crew-set count","Needs crew-set/roster variables and owner-confirmed priority order.")]
 rows=[]
 for term,priority,params,analogue,blocker in data:
  names={"retiming_penalty":("retime_base","retiming_cost_tier_1","retiming_cost_tier_2","retiming_cost_tier_3"),
         "crew_efficiency_penalty":("unutilized_hub_time_cost_scaling","unutilized_spoke_time_cost_scaling","fdp_wasted_cost_scaling"),
         "crew_set_count":("man_day_cost",)}.get(term,())
  rows.append({"Term":term,"Proposed priority (confirmation pending)":priority,"Source cost parameters":params,
   "Loaded values":json.dumps({k:costs.get(k) for k in names},sort_keys=True),"Current generic-MIP analogue":analogue,
   "Status":"BLOCKED_NOT_RUN","Blocker":blocker,"Numeric operational component":None})
 return pd.DataFrame(rows)
def run(repo_root,output_dir):
 root=Path(repo_root);cost=next((p for p in (root/"originals"/"cost_parameters.csv",root/"cost_parameters.csv") if p.is_file()),None)
 costs=load_cost_parameters(cost) if cost else {};found=[x for x in CANDIDATES if (root/x).is_file()]
 baseline,summaries=baseline_breakdown(root);components=readiness_rows(costs)
 report={"operational_solver_present":bool(found),"operational_solver_files_found":found,"files_checked":list(CANDIDATES),
  "archived_generic_mip_present":(root/"patches"/"mip.py.orig").is_file(),
  "finding":"No active production column-generation/aircraft-timeline solver found. patches/mip.py.orig is an archived generic coverage MIP, not the operational solver.",
  "cost_parameter_file":str(cost) if cost else None,"cost_parameters_loaded":len(costs),"four_term_objective_status":"BLOCKED_NOT_RUN",
  "baseline_summaries_found":sorted(summaries),"objective_contract":objective_contract()}
 out=Path(output_dir);out.mkdir(parents=True,exist_ok=True)
 baseline.to_csv(out/"baseline_objective_breakdown.csv",index=False);components.to_csv(out/"objective_component_readiness.csv",index=False)
 (out/"objective_readiness.json").write_text(json.dumps(report,indent=2,sort_keys=True),encoding="utf-8")
 print(json.dumps({"operational_solver_present":report["operational_solver_present"],"four_term_objective_status":report["four_term_objective_status"],
  "baseline_objective_breakdown":baseline.to_dict(orient="records"),"outputs":str(out)},indent=2,default=str))
 return report
def main():
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument("--repo-root",type=Path,default=ROOT);ap.add_argument("--output-dir",type=Path,default=ROOT/"output"/"objective_readiness")
 a=ap.parse_args();run(a.repo_root,a.output_dir);return 0
if __name__=="__main__":raise SystemExit(main())
