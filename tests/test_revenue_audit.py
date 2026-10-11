import pandas as pd
from revenue_audit import audit_data,market_route
def schedule(rows):return pd.DataFrame(rows,columns=["Day","Flt Num","From","To","Eqp"])
def revenue(rows):return pd.DataFrame(rows,columns=["Routes","Aircraft Type Code","DoW","Avg_PLF","Avg_Pax_Fare_SGD"])
def test_market_route_direction():
 assert market_route("SIN","BKK")==("SINBKK",True) and market_route("BKK","SIN")==("SINBKK",True)
 assert market_route("BKK","KUL")==("BKKKUL",False)
def test_exact_join_and_no_guessed_equipment_crosswalk():
 s=schedule([["2026-10-25",1,"SIN","BKK","320"],["2026-10-25",2,"BKK","SIN","320"],["2026-10-25",3,"SIN","BKK","32Q"]])
 r=revenue([["SINBKK","320","Sunday",.85,1000]])
 summary,unmatched,_,_=audit_data(s,r)
 assert summary["join_metrics"]["market_route_plus_equipment_plus_weekday"]["matched_rows"]==2
 assert summary["join_metrics"]["directed_route_plus_equipment_plus_weekday"]["matched_rows"]==1
 assert "EQUIPMENT_CODE_MISSING_CROSSWALK" in set(unmatched["Reason"])
def test_duplicate_revenue_keys_are_ambiguous_and_gate_is_off():
 s=schedule([["2026-10-25",1,"SIN","BKK","320"]])
 r=revenue([["SINBKK","320","Sunday",.8,1000],["SINBKK","320","Sunday",.9,1100]])
 summary,unmatched,_,_=audit_data(s,r)
 assert summary["join_metrics"]["market_route_plus_equipment_plus_weekday"]["matched_rows"]==0
 assert unmatched.iloc[0]["Reason"]=="AMBIGUOUS_DUPLICATE_REVENUE_KEY"
 assert summary["revenue_penalty_gate"]["enabled"] is False
def test_anomaly_screens_do_not_modify_data():
 s=schedule([["2026-10-25",1,"SIN","BKK","320"]]);r=revenue([["SINBKK","320","Sunday",1.08,125000]])
 _,_,anomalies,_=audit_data(s,r)
 assert {"PLF_ABOVE_1","FARE_ABOVE_100000_SGD_SCREEN"}.issubset(set(anomalies["Anomaly"]))
