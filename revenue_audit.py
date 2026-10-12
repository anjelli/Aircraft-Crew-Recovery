#!/usr/bin/env python3
"""Audit schedule-to-revenue joins; never invent direction or equipment crosswalks."""
from __future__ import annotations
import argparse, json, re
from collections import Counter
from pathlib import Path
from typing import Any
import pandas as pd

ROOT = Path(__file__).resolve().parent
SCHEDULE_COLUMNS = {"Day", "Flt Num", "From", "To", "Eqp"}
REVENUE_COLUMNS = {"Routes", "Aircraft Type Code", "DoW", "Avg_PLF", "Avg_Pax_Fare_SGD"}
FARE_SCREEN_SGD = 100_000.0

def key(value: Any) -> str:
    if pd.isna(value): return ""
    return re.sub(r"[^A-Z0-9]", "", str(value).strip().upper())

def market_route(origin: Any, destination: Any) -> tuple[str, bool]:
    """Normalize hub-spoke direction to SIN+spoke. Owner confirmation is required."""
    a, b = key(origin), key(destination)
    if a == "SIN" and b: return "SIN" + b, True
    if b == "SIN" and a: return "SIN" + a, True
    return a + b, False

def audit_data(schedule: pd.DataFrame, revenue: pd.DataFrame):
    missing_s = sorted(SCHEDULE_COLUMNS - set(schedule.columns))
    missing_r = sorted(REVENUE_COLUMNS - set(revenue.columns))
    if missing_s: raise ValueError(f"Schedule missing required columns: {missing_s}")
    if missing_r: raise ValueError(f"Revenue workbook missing required columns: {missing_r}")
    s, r = schedule.copy(), revenue.copy()
    s["_direct_route"] = [key(a) + key(b) for a,b in zip(s["From"], s["To"])]
    market_values = [market_route(a,b) for a,b in zip(s["From"],s["To"])]
    s["_market_route"] = [x[0] for x in market_values]
    s["_has_sin"] = [x[1] for x in market_values]
    s["_eqp"] = s["Eqp"].map(key)
    dates = pd.to_datetime(s["Day"], errors="coerce")
    s["_dow"] = dates.dt.day_name()
    r["_route"] = r["Routes"].map(key)
    r["_eqp"] = r["Aircraft Type Code"].map(key)
    r["_dow"] = r["DoW"].astype("string").str.strip().str.title()
    rkeys = list(zip(r["_route"],r["_eqp"],r["_dow"]))
    counts = Counter(rkeys)
    dup = {k for k,n in counts.items() if n > 1}
    unique_rkeys = {k for k,n in counts.items() if n == 1 and all(k)}
    revenue_routes = {k[0] for k in counts if k[0]}
    revenue_types = {k[1] for k in counts if k[1]}
    route_eqp = {(k[0],k[1]) for k in counts if k[0] and k[1]}
    route_dow = {(k[0],k[2]) for k in counts if k[0] and k[2]}
    direct = list(zip(s["_direct_route"],s["_eqp"],s["_dow"]))
    market = list(zip(s["_market_route"],s["_eqp"],s["_dow"]))
    s["_direct_route_match"] = s["_direct_route"].isin(revenue_routes)
    s["_market_route_match"] = s["_market_route"].isin(revenue_routes)
    s["_direct_re_match"] = [(a,b) in route_eqp for a,b,_ in direct]
    s["_market_re_match"] = [(a,b) in route_eqp for a,b,_ in market]
    s["_direct_exact"] = [k in unique_rkeys for k in direct]
    s["_exact"] = [k in unique_rkeys for k in market]
    def reason(k, exact):
        if exact: return "MATCHED"
        route, eqp, dow = k
        if k in dup: return "AMBIGUOUS_DUPLICATE_REVENUE_KEY"
        if not route or not eqp or not dow: return "MISSING_SCHEDULE_JOIN_FIELD"
        if route not in revenue_routes: return "ROUTE_MARKET_NOT_IN_REVENUE"
        if eqp not in revenue_types: return "EQUIPMENT_CODE_MISSING_CROSSWALK"
        if (route,eqp) not in route_eqp: return "ROUTE_EQUIPMENT_COMBINATION_MISSING"
        if (route,dow) not in route_dow: return "WEEKDAY_KEY_MISSING"
        return "UNCLASSIFIED_JOIN_FAILURE"
    s["_reason"] = [reason(k,m) for k,m in zip(market,s["_exact"])]
    bad = s.loc[~s["_exact"]]
    if len(bad):
        unmatched = bad.groupby(["_market_route","_eqp","_dow","_reason"],dropna=False).agg(
          **{"Schedule Rows":("Flt Num","size"),
             "Example Flights":("Flt Num",lambda x:"|".join(str(v) for v in x.head(5))),
             "Example Dates":("Day",lambda x:"|".join(str(v)[:10] for v in x.head(5)))}
        ).reset_index().rename(columns={"_market_route":"Market Route","_eqp":"Schedule Eqp","_dow":"Day of Week","_reason":"Reason"})
        unmatched = unmatched.sort_values(["Reason","Schedule Rows","Market Route","Schedule Eqp","Day of Week"],ascending=[True,False,True,True,True])
    else:
        unmatched = pd.DataFrame(columns=["Market Route","Schedule Eqp","Day of Week","Reason","Schedule Rows","Example Flights","Example Dates"])
    equipment_rows=[]
    for eqp,g in s.groupby("_eqp",dropna=False):
        n=len(g); matched=int(g["_exact"].sum())
        equipment_rows.append({"Schedule Equipment Code":eqp,"Schedule Rows":n,"Exact Route+Equipment+DoW Matches":matched,
           "Exact Match Rate %":round(100*matched/max(1,n),3),"Code Exists in Revenue":bool(eqp in revenue_types),"Crosswalk Assumed":False})
    equipment=pd.DataFrame(equipment_rows).sort_values("Schedule Equipment Code").reset_index(drop=True)
    plf=pd.to_numeric(r["Avg_PLF"],errors="coerce"); fare=pd.to_numeric(r["Avg_Pax_Fare_SGD"],errors="coerce")
    q1,q3=fare.quantile(.25),fare.quantile(.75); iqr_high=q3+1.5*(q3-q1); iqr_low=q1-1.5*(q3-q1)
    anomaly_rows=[]
    for idx,row in r.iterrows():
        p,f=plf.loc[idx],fare.loc[idx]; flags=[]
        if pd.isna(p): flags.append("PLF_MISSING_OR_NONNUMERIC")
        elif p>1: flags.append("PLF_ABOVE_1")
        elif p<0: flags.append("PLF_BELOW_0")
        if pd.isna(f): flags.append("FARE_MISSING_OR_NONNUMERIC")
        elif f<=0: flags.append("FARE_NONPOSITIVE")
        else:
            if f>iqr_high or f<iqr_low: flags.append("FARE_IQR_OUTLIER")
            if f>FARE_SCREEN_SGD: flags.append("FARE_ABOVE_100000_SGD_SCREEN")
        k=(row["_route"],row["_eqp"],row["_dow"])
        if not all(k): flags.append("INCOMPLETE_REVENUE_JOIN_KEY")
        if k in dup: flags.append("DUPLICATE_REVENUE_JOIN_KEY")
        for flag in flags:
            anomaly_rows.append({"Revenue Row (Excel)":int(idx)+2,"Routes":row["Routes"],"Aircraft Type Code":row["Aircraft Type Code"],
              "DoW":row["DoW"],"Avg_PLF":row["Avg_PLF"],"Avg_Pax_Fare_SGD":row["Avg_Pax_Fare_SGD"],"Anomaly":flag,
              "Note":"Screening flag only; not a business rule."})
    anomalies=pd.DataFrame(anomaly_rows,columns=["Revenue Row (Excel)","Routes","Aircraft Type Code","DoW","Avg_PLF","Avg_Pax_Fare_SGD","Anomaly","Note"])
    n=len(s); direct_route=int(s["_direct_route_match"].sum()); direct_re=int(s["_direct_re_match"].sum())
    direct_exact=int(s["_direct_exact"].sum()); market_route_rows=int(s["_market_route_match"].sum())
    market_re=int(s["_market_re_match"].sum()); exact=int(s["_exact"].sum())
    markets=set(s["_market_route"]); schedule_keys=set(market); unique_matched=len(schedule_keys & unique_rkeys)
    summary={
      "inputs":{"schedule_rows":n,"revenue_rows":len(r),"schedule_missing_or_invalid_dates":int(dates.isna().sum())},
      "join_assumptions":{"market_route":"For hub-spoke routes, inbound/outbound both map to SIN+spoke; confirm with data owner.",
         "equipment":"Exact equipment codes only; no 32P/32Q/321 mapping inferred.","weekday":"Schedule Day is treated as local service/departure date and joined to revenue DoW."},
      "join_metrics":{
       "directed_route_only":{"matched_rows":direct_route,"total_rows":n,"match_rate_pct":round(100*direct_route/max(1,n),3),
        "unique_schedule_routes":int(s["_direct_route"].nunique()),"matched_unique_schedule_routes":int(s.loc[s["_direct_route_match"],"_direct_route"].nunique())},
       "directed_route_plus_equipment":{"matched_rows":direct_re,"total_rows":n,"match_rate_pct":round(100*direct_re/max(1,n),3)},
       "directed_route_plus_equipment_plus_weekday":{"matched_rows":direct_exact,"total_rows":n,"match_rate_pct":round(100*direct_exact/max(1,n),3),"unmatched_rows":n-direct_exact},
       "market_route_only":{"matched_rows":market_route_rows,"total_rows":n,"match_rate_pct":round(100*market_route_rows/max(1,n),3),
        "schedule_markets":len(markets),"matched_schedule_markets":len(markets & revenue_routes),"revenue_markets_not_in_schedule":len(revenue_routes-markets)},
       "market_route_plus_equipment":{"matched_rows":market_re,"total_rows":n,"match_rate_pct":round(100*market_re/max(1,n),3)},
       "market_route_plus_equipment_plus_weekday":{"matched_rows":exact,"total_rows":n,"match_rate_pct":round(100*exact/max(1,n),3),
        "unmatched_rows":n-exact,"unique_schedule_keys":len(schedule_keys),"matched_unique_keys":unique_matched,
        "unique_key_match_rate_pct":round(100*unique_matched/max(1,len(schedule_keys)),3),"duplicate_revenue_keys":len(dup),
        "route_equipment_rows_missing_weekday":int((s["_market_re_match"] & ~s["_exact"]).sum())}},
      "unmatched_reason_counts_schedule_rows":{str(k):int(v) for k,v in s.loc[~s["_exact"],"_reason"].value_counts().items()},
      "revenue_quality":{"rows":len(r),"distinct_route_equipment_weekday_keys":len(set(rkeys)),"duplicate_extra_key_rows":len(r)-len(set(rkeys)),
        "nulls":{str(k):int(v) for k,v in r[list(REVENUE_COLUMNS)].isna().sum().items()},
        "PLF_above_1_count":int((plf>1).sum()),"PLF_below_0_count":int((plf<0).sum()),"PLF_min":float(plf.min()),
        "PLF_median":float(plf.median()),"PLF_max":float(plf.max()),"fare_missing_or_nonnumeric":int(fare.isna().sum()),
        "fare_nonpositive":int((fare<=0).sum()),"fare_min":float(fare.min()),"fare_p25":float(q1),"fare_median":float(fare.median()),
        "fare_p75":float(q3),"fare_IQR_upper_screen":float(iqr_high),"fare_IQR_outlier_count":int(((fare>iqr_high)|(fare<iqr_low)).sum()),
        "fare_above_100000_SGD_screen_count":int((fare>FARE_SCREEN_SGD).sum()),"fare_max":float(fare.max()),
        "fare_units":"UNCONFIRMED; do not use for cancellation penalty."},
      "revenue_penalty_gate":{"enabled":False,"reason":"Owner must confirm route convention, fare units, PLF anomalies, and equipment-code crosswalk before deriving per-flight penalties."}}
    return summary,unmatched.reset_index(drop=True),anomalies,equipment

def run_audit(schedule_path:Path,revenue_path:Path,output_dir:Path):
    summary,unmatched,anomalies,equipment=audit_data(pd.read_excel(schedule_path),pd.read_excel(revenue_path))
    output_dir.mkdir(parents=True,exist_ok=True)
    (output_dir/"revenue_join_audit.json").write_text(json.dumps(summary,indent=2,sort_keys=True,allow_nan=False),encoding="utf-8")
    unmatched.to_csv(output_dir/"revenue_unmatched_schedule_keys.csv",index=False)
    anomalies.to_csv(output_dir/"revenue_anomalies_detailed.csv",index=False)
    equipment.to_csv(output_dir/"revenue_equipment_coverage.csv",index=False)
    print(json.dumps({"join_metrics":summary["join_metrics"],"unmatched_reason_counts_schedule_rows":summary["unmatched_reason_counts_schedule_rows"],
      "revenue_quality":summary["revenue_quality"],"outputs":str(output_dir)},indent=2,sort_keys=True,default=str))
    return summary

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument("--schedule",type=Path,default=None);ap.add_argument("--revenue",type=Path,default=None)
    ap.add_argument("--output-dir",type=Path,default=ROOT/"output"/"revenue_audit");args=ap.parse_args(argv)
    schedule=args.schedule or (ROOT/"input"/"schedule.xlsx" if (ROOT/"input"/"schedule.xlsx").is_file() else ROOT/"originals"/"schedule_kgp.xlsx")
    revenue=args.revenue or ROOT/"originals"/"revenue.xlsx"
    if not schedule.is_file(): ap.error(f"Schedule not found: {schedule}")
    if not revenue.is_file(): ap.error(f"Revenue workbook not found: {revenue}")
    run_audit(schedule,revenue,args.output_dir);return 0
if __name__=="__main__": raise SystemExit(main())
