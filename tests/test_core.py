import sys, importlib.util
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd, pytest
ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))
import independent_validator as iv
O = ROOT / "originals"

def mk(fid, frm, to, std, blk_min, eqp="320", mgt=50, active=True):
    s = datetime.fromisoformat(std); a = s + timedelta(minutes=blk_min)
    return dict(flight_id=fid, From=frm, To=to, STD=s, STA=a, STD_local=s, STA_local=a, Eqp=eqp, mgt_min=mgt, mgt_status="L1", active=active)

FDP = iv.load_fdp(O / "fdp_rules.csv")
R = iv.Rules(fdp=FDP, dsb=60, feb=0, deb=30)

# ---------- FDP / connection / equipment
def test_fdp_band_edges():
    assert iv.fdp_band(datetime(2026,10,25,5,59)) == "2200-0559"
    assert iv.fdp_band(datetime(2026,10,25,6,0)) == "0600-0759"
    assert iv.fdp_band(datetime(2026,10,25,14,59)) == "0800-1459"
    assert iv.fdp_band(datetime(2026,10,25,22,0)) == "2200-0559"

def test_legal_two_sector_turn():
    d = [mk("a","SIN","KUL","2026-10-25T09:00",60), mk("b","KUL","SIN","2026-10-25T11:00",60)]
    assert iv.check_duty("p", d, R) == []

def test_fdp_limit_violation():
    # 2 sectors starting 08:00 band 0800-1459 limit 13.25h; long ground time pushes duty past it
    d = [mk("a","SIN","KUL","2026-10-25T09:00",60), mk("b","KUL","SIN","2026-10-25T22:30",60)]
    assert [v.vtype for v in iv.check_duty("p", d, R)] == ["FDP_LIMIT"]

def test_mgt_connection_violation_and_exact_boundary():
    ok  = [mk("a","SIN","KUL","2026-10-25T09:00",60), mk("b","KUL","SIN","2026-10-25T10:50",60)]  # gap 50 == mgt
    bad = [mk("a","SIN","KUL","2026-10-25T09:00",60), mk("b","KUL","SIN","2026-10-25T10:49",60)]
    assert iv.check_duty("p", ok, R) == []
    assert [v.vtype for v in iv.check_duty("p", bad, R)] == ["MGT_CONNECTION"]

def test_equipment_swap_after_sector_two_needs_90():
    f = [mk("a","SIN","KUL","2026-10-25T09:00",60), mk("b","KUL","SIN","2026-10-25T11:00",60),
         mk("c","SIN","BKK","2026-10-25T12:20",60,eqp="32Q")]  # gap 20 < 50 anyway -> use 80 min gap
    f[2] = mk("c","SIN","BKK","2026-10-25T12:20",60,eqp="32Q")
    f[1]["STA"] = f[1]["STA_local"] = datetime(2026,10,25,12,0)
    f[2]["STD"] = f[2]["STD_local"] = datetime(2026,10,25,13,20); f[2]["STA"]=f[2]["STA_local"]=datetime(2026,10,25,14,20)  # gap 80
    assert "MGT_CONNECTION" in [v.vtype for v in iv.check_duty("p", f, R)]      # 80 < 90 (eqp change)
    f[2]["Eqp"] = "320"
    assert "MGT_CONNECTION" not in [v.vtype for v in iv.check_duty("p", f, R)]  # 80 >= 50 (same eqp)

def test_hub_turn_equipment_continuity_first_connection():
    d = [mk("a","SIN","KUL","2026-10-25T09:00",60,eqp="320"), mk("b","KUL","SIN","2026-10-25T11:00",60,eqp="32Q")]
    assert "EQUIPMENT_CONTINUITY" in [v.vtype for v in iv.check_duty("p", d, R)]

def test_cancelled_flight_flagged():
    seq = [mk("a","SIN","KUL","2026-10-25T09:00",60,active=False)]
    assert iv.check_pairing("p", seq, [[0]], R)[0].vtype == "CANCELLED_FLIGHT"

def test_rest_rules():
    a = [mk("a","SIN","CGO","2026-10-25T09:00",300)]
    ok  = [mk("b","CGO","SIN","2026-10-26T06:00",300)]   # ~16h rest
    bad = [mk("b","CGO","SIN","2026-10-25T20:00",300)]   # ~6h rest
    assert iv.check_rest("p", a, ok, R) == []
    assert iv.check_rest("p", a, bad, R)[0].vtype == "REST_MINIMUM"
    far = [mk("b","CGO","SIN","2026-10-30T06:00",300)]
    assert iv.check_rest("p", a, far, R)[0].vtype == "MAX_LAYOVER"

# ---------- pairing report handling (ffill regression)
def test_blank_pairing_ids_are_not_forward_filled():
    rep = pd.DataFrame({"Pairing ID":[1.0,1.0,None,None],"Flight Date":pd.to_datetime(["2026-10-25"]*4),
                        "Duty Start":["07:00",None,None,None],"Flight Num":[1,2,3,4],"STD_local":["08:00:00"]*4})
    groups = list(iv.pairing_groups(rep))
    assert len(groups) == 1 and len(groups[0][1]) == 2

# ---------- MGT resolver
def _mgt_file(tmp_path, rows):
    cols = ["Type","Start Date","End Date","Freq","Cxr","Via Lvl","Via Arpt","Eqp Lvl","Eqp","Arr Start","Arr End","MGT"]
    p = tmp_path / "m.xlsx"; pd.DataFrame(rows, columns=cols).to_excel(p, index=False); return p

def test_mgt_precedence_airport_over_country_over_default(tmp_path):
    base = ["Standard","2026-10-25","2027-03-27",1234567,"TR"]
    rows = [base+["Airport","","Subfleet","320",None,None,45],
            base+["Country","CN","Subfleet","320",None,None,60],
            base+["Airport","CGO","Subfleet","320",None,None,70]]
    r = iv.MGTResolver(_mgt_file(tmp_path, rows))
    d = pd.Timestamp("2026-11-01")
    assert r.resolve("320","CGO","CN",d)[0] == 70
    assert r.resolve("320","XIY","CN",d)[0] == 60
    assert r.resolve("320","KUL","MY",d)[0] == 45

def test_mgt_expired_rules_not_silently_applied():
    r = iv.MGTResolver(O / "MGT.xlsx")
    assert r.resolve("320","SIN","SG",pd.Timestamp("2027-03-27"))[1] != "NO_RULE_IN_EFFECT"
    assert r.resolve("320","SIN","SG",pd.Timestamp("2027-03-28")) == (None,"NO_RULE_IN_EFFECT")

# ---------- disruption reproducibility (uses the real experiment module)
def _load_exp():
    spec = importlib.util.spec_from_file_location("exp", ROOT/"run_pairing_experiment.py"); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def test_disruption_is_reproducible_and_in_range():
    exp = _load_exp()
    s = pd.read_excel(ROOT/"input"/"schedule.xlsx").head(500)
    for c in ("Day","STD","STA","STD_sin","STA_sin"): s[c] = pd.to_datetime(s[c])
    s["Eqp"] = s["Eqp"].astype(str); s["flight_id"] = s["Flt Num"].astype(str)+"_"+s["STD"].dt.strftime("%Y%m%dT%H%M%S")
    a = exp.simulate_disruption(s, [5,10,15], 2026, "1W"); b = exp.simulate_disruption(s, [5,10,15], 2026, "1W")
    c = exp.simulate_disruption(s, [5,10,15], 7, "1W")
    assert a.equals(b)
    assert not a[["flight_id","disruption_status"]].equals(c[["flight_id","disruption_status"]])
    pct = (a.disruption_status != "UNCHANGED").mean()*100
    assert 5 <= pct <= 10
    orig = s.set_index("flight_id").loc[a.flight_id]["STD"].values        # original schedule object untouched
    assert (s["STD"].values == s["STD"].values).all() and len(orig) == len(a)

# ---------- integration regression pinned to measured numbers (skipped if outputs absent)
@pytest.mark.skipif(not (ROOT/"output"/"baseline"/"baseline_summary.json").exists(), reason="run run_baseline_validation.py first")
def test_baseline_numbers_pinned():
    import json
    b = json.load(open(ROOT/"output"/"baseline"/"baseline_summary.json"))
    assert b["schedule_flights"] == 19238
    assert b["baseline_full_155d"]["pairings_checked"] == 8473 and b["baseline_full_155d"]["unmatched"] == 0
    assert b["baseline_full_155d"]["by_type"] == {"MAX_LAYOVER": 27, "MGT_RULE_MISSING": 2}
    assert b["mgt_cross_check"]["mismatches"] == 16
