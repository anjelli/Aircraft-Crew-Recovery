#!/usr/bin/env python3
"""
Independent pairing-legality validator.

Deliberately does NOT import pairings_builder.py or MGT.py. Rules are re-implemented
from the rule *data* (fdp_rules.csv, cost_parameters.csv, MGT.xlsx) and the rule
*semantics* documented in the builder source, so that bugs in the builder's own
functions cannot hide themselves.

Semantics mirrored (and flagged where questionable) -- see ARCHITECTURE.md:
  * duty start  = first STD(SIN clock) - duty_start_buffer
  * duty end    = last STA(SIN clock) + (reported_duty_end_buffer if sectors>=4 else fdp_end_buffer)
  * FDP limit   = fdp_rules[band of local duty start][min(sectors,8)-1]
  * connection  = gap >= MGT(prev arrival); after the 2nd sector an equipment change needs 90 min
  * hub-turn duties (SIN..SIN) obey the single-day equipment-continuity rule
  * rest        = next first STD - prev last STA, >0 and <= max layover (DEMO 72h)
                  required: prev duty >16h -> >=24h & local night; >10h -> >=ceil(h); else >=10h (local night) / 12h
ASSUMPTIONS (not official): AIRCRAFT_GROUPS_DEMO, max layover 72 h, layover-station list.
"""
from __future__ import annotations
import math
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
import pandas as pd

HUB = "SIN"
MAX_SECTORS = 4
EQP_CHANGE_MGT = timedelta(minutes=90)
AIRCRAFT_GROUPS_DEMO = {"320": {"320", "32N", "32Q"}, "787": set()}  # DEMO ASSUMPTION
MAX_LAYOVER_H = 72.0  # DEMO ASSUMPTION


# ---------------------------------------------------------------- rule data
def load_fdp(path) -> dict:
    d = pd.read_csv(path, encoding="utf-8-sig")
    return {r["time_window"]: [float(r[f"limit{i}"]) for i in range(1, 9)] for _, r in d.iterrows()}


def load_costs(path) -> dict:
    d = pd.read_csv(path, encoding="utf-8-sig")
    return {r["parameter"]: float(r["value"]) for _, r in d.iterrows() if str(r["value"]).replace(".", "", 1).isdigit()}


def fdp_band(local_dt) -> str:
    h = local_dt.hour
    if 6 <= h < 8:
        return "0600-0759"
    if 8 <= h < 15:
        return "0800-1459"
    if 15 <= h < 22:
        return "1500-2159"
    return "2200-0559"


# ---------------------------------------------------------------- MGT resolver (independent)
class MGTResolver:
    """6-level precedence (Subfleet+Airport, Group+Airport, Subfleet+Country, Group+Country,
    Subfleet default, Group default). Unlike MGT.py it honours Start/End Date, Freq and Cxr and
    returns status NO_RULE_IN_EFFECT instead of silently applying an expired rule."""

    def __init__(self, mgt_xlsx, groups=AIRCRAFT_GROUPS_DEMO, carrier="TR"):
        m = pd.read_excel(mgt_xlsx)
        m = m[m["Cxr"].astype(str).str.upper() == carrier].copy()
        for c in ("Eqp", "Via Arpt", "Via Lvl", "Eqp Lvl"):
            m[c] = m[c].fillna("").astype(str).str.strip().str.upper()
        m["Start Date"] = pd.to_datetime(m["Start Date"]).dt.normalize()
        m["End Date"] = pd.to_datetime(m["End Date"]).dt.normalize()
        m["Freq"] = m["Freq"].astype(str)
        m["mins"] = m["MGT"].apply(self._parse)
        self.m, self.groups, self.cache = m.reset_index(drop=True), groups, {}

    @staticmethod
    def _parse(v):  # MGT column is digits 'hmm' ("130" -> 90 min per MGT.py convention, "50" -> 50)
        s = str(int(float(v))).zfill(3)
        return int(s[0]) * 60 + int(s[1:])

    def resolve(self, eqp, airport, country, on_date) -> tuple[int | None, str]:
        key = (eqp, airport, country, pd.Timestamp(on_date).normalize())
        if key in self.cache:
            return self.cache[key]
        eqp_u, ap, ct = str(eqp).upper(), str(airport).upper(), str(country).upper()
        d = key[3]
        dow = str(d.isoweekday())
        m = self.m
        live = m[(m["Start Date"] <= d) & (m["End Date"] >= d) & m["Freq"].str.contains(dow)]
        if live.empty:
            res = (None, "NO_RULE_IN_EFFECT")
            self.cache[key] = res
            return res
        grp = lambda g: eqp_u in self.groups.get(g, set())
        levels = [
            (live["Eqp Lvl"].eq("SUBFLEET") & live["Eqp"].eq(eqp_u) & live["Via Lvl"].eq("AIRPORT") & live["Via Arpt"].eq(ap)),
            (live["Eqp Lvl"].eq("AIRCRAFT GROUP") & live["Via Lvl"].eq("AIRPORT") & live["Via Arpt"].eq(ap) & live["Eqp"].map(grp)),
            (live["Eqp Lvl"].eq("SUBFLEET") & live["Eqp"].eq(eqp_u) & live["Via Lvl"].eq("COUNTRY") & live["Via Arpt"].eq(ct)),
            (live["Eqp Lvl"].eq("AIRCRAFT GROUP") & live["Via Lvl"].eq("COUNTRY") & live["Via Arpt"].eq(ct) & live["Eqp"].map(grp)),
            (live["Eqp Lvl"].eq("SUBFLEET") & live["Eqp"].eq(eqp_u) & live["Via Arpt"].eq("")),
            (live["Eqp Lvl"].eq("AIRCRAFT GROUP") & live["Via Arpt"].eq("") & live["Eqp"].map(grp)),
        ]
        for i, mask in enumerate(levels, start=1):
            hit = live[mask]
            if len(hit):
                vals = set(hit["mins"])
                res = (int(hit.iloc[0]["mins"]), f"L{i}" + ("_CONFLICT" if len(vals) > 1 else ""))
                self.cache[key] = res
                return res
        res = (None, "NO_MATCHING_RULE")
        self.cache[key] = res
        return res


# ---------------------------------------------------------------- violations
@dataclass
class Violation:
    pairing_id: str
    flight_id: str
    vtype: str
    value: object
    limit: object
    detail: str = ""

    def as_dict(self):
        return {"Pairing ID": self.pairing_id, "Flight ID": self.flight_id, "Violation Type": self.vtype,
                "Value": self.value, "Limit": self.limit, "Detail": self.detail}


@dataclass
class Rules:
    fdp: dict
    dsb: int = 60
    feb: int = 0
    deb: int = 30
    max_layover_h: float = MAX_LAYOVER_H
    strict_local_night: bool = False  # SENSITIVITY ONLY: 'and' instead of builder's 'or'


def _duty_times(duty, r: Rules):
    n = len(duty)
    ds = duty[0]["STD"] - timedelta(minutes=r.dsb)
    de = duty[-1]["STA"] + timedelta(minutes=(r.deb if n >= MAX_SECTORS else r.feb))
    return ds, de, (de - ds).total_seconds() / 3600.0


def check_duty(pid, duty, r: Rules, mgt_status_is_missing=lambda f: False):
    out = []
    n = len(duty)
    if n > MAX_SECTORS:
        out.append(Violation(pid, duty[-1]["flight_id"], "SECTOR_LIMIT", n, MAX_SECTORS))
    ds, de, hours = _duty_times(duty, r)
    ds_local = duty[0]["STD_local"] - timedelta(minutes=r.dsb)
    band = fdp_band(ds_local)
    lim = r.fdp[band][min(n, 8) - 1]
    if hours > lim + 1e-9:
        out.append(Violation(pid, duty[-1]["flight_id"], "FDP_LIMIT", round(hours, 2), lim, band))
    for k in range(1, n):
        a, b = duty[k - 1], duty[k]
        if a["To"] != b["From"]:
            out.append(Violation(pid, b["flight_id"], "CONNECTIVITY", f"{a['To']}->{b['From']}", "same station"))
            continue
        if a["mgt_min"] is None or pd.isna(a["mgt_min"]):
            out.append(Violation(pid, b["flight_id"], "MGT_RULE_MISSING", a.get("mgt_status"), "rule in effect"))
            continue
        need = timedelta(minutes=a["mgt_min"])
        if k == 2 and a["Eqp"] != b["Eqp"]:
            need = EQP_CHANGE_MGT
        gap = b["STD"] - a["STA"]
        if gap < need:
            out.append(Violation(pid, b["flight_id"], "MGT_CONNECTION", round(gap.total_seconds() / 60, 1), need.total_seconds() / 60))
    if duty[0]["From"] == HUB and duty[-1]["To"] == HUB:
        s_eqp = duty[0]["Eqp"]
        for k in range(1, n):
            cur, nxt = duty[k - 1]["Eqp"], duty[k]["Eqp"]
            ok = (nxt == s_eqp) if k == 1 else True if k == 2 else (nxt == cur) if k == 3 else False
            if not ok:
                out.append(Violation(pid, duty[k]["flight_id"], "EQUIPMENT_CONTINUITY", f"{cur}->{nxt}", f"start {s_eqp}"))
    return out


def check_rest(pid, a, b, r: Rules):
    gap = b[0]["STD"] - a[-1]["STA"]
    gh = gap.total_seconds() / 3600
    if a[-1]["To"] != b[0]["From"]:
        return [Violation(pid, b[0]["flight_id"], "DUTY_BOUNDARY_STATION", f"{a[-1]['To']}->{b[0]['From']}", "same station")]
    if gap <= timedelta(0):
        return [Violation(pid, b[0]["flight_id"], "NON_POSITIVE_REST", round(gh, 2), 0)]
    if gh > r.max_layover_h:
        return [Violation(pid, b[0]["flight_id"], "MAX_LAYOVER", round(gh, 2), r.max_layover_h, "DEMO ASSUMPTION")]
    prev_h = _duty_times(a, r)[2]
    s2, e1 = b[0]["STD_local"], a[-1]["STA_local"]
    night = gh >= 10 and ((s2.hour >= 8 and e1.hour <= 22) if r.strict_local_night else (s2.hour >= 8 or e1.hour <= 22))
    if prev_h > 16:
        ok, need = (gh >= 24 and night), "24h+local night"
    elif prev_h > 10:
        need = math.ceil(prev_h)
        ok = gh >= need
    else:
        need = 10 if night else 12
        ok = gh >= need
    return [] if ok else [Violation(pid, b[0]["flight_id"], "REST_MINIMUM", round(gh, 2), need, f"prev duty {prev_h:.2f}h")]


def check_pairing(pid, seq, duty_splits, r: Rules):
    out = []
    for f in seq:
        if not f.get("active", True):
            out.append(Violation(pid, f["flight_id"], "CANCELLED_FLIGHT", "CANCELLED", "ACTIVE"))
    if out:
        return out
    duties = [[seq[i] for i in idx] for idx in duty_splits]
    for d in duties:
        out += check_duty(pid, d, r)
    for i in range(len(duties) - 1):
        out += check_rest(pid, duties[i], duties[i + 1], r)
    return out


# ---------------------------------------------------------------- schedule preparation
def prepare_schedule(schedule: pd.DataFrame, resolver: MGTResolver) -> pd.DataFrame:
    s = schedule.copy()
    for c in ("Day", "STD", "STA", "STD_sin", "STA_sin"):
        s[c] = pd.to_datetime(s[c])
    s = s.rename(columns={"STD": "STD_local", "STA": "STA_local", "STD_sin": "STD", "STA_sin": "STA"})
    s["Eqp"] = s["Eqp"].astype(str).str.strip()
    s["flight_id"] = s["Flt Num"].astype(int).astype(str) + "_" + s["STD_local"].dt.strftime("%Y%m%dT%H%M%S")
    res = [resolver.resolve(e, t, c, d) for e, t, c, d in zip(s.Eqp, s.To, s["Arvl Ctry"], s.STA_local.dt.normalize())]
    s["mgt_min"] = [x[0] for x in res]
    s["mgt_status"] = [x[1] for x in res]
    s["active"] = True
    return s


def pairing_groups(report: pd.DataFrame):
    """Yield (pairing_id, ordered rows, duty_splits). Blank Pairing IDs are NOT forward-filled."""
    rep = report[report["Pairing ID"].notna()].copy()
    rep["Flight Date"] = pd.to_datetime(rep["Flight Date"]).dt.normalize()
    for pid, g in rep.groupby("Pairing ID", sort=False):
        g = g.reset_index(drop=True)
        idx, cur = [], []
        for i, ds in enumerate(g["Duty Start"].notna()):
            if ds and cur:
                idx.append(cur)
                cur = []
            cur.append(i)
        if cur:
            idx.append(cur)
        yield str(int(pid)), g, idx


def report_flight_id(row) -> str:
    t = pd.Timestamp(str(row["STD_local"])) if not hasattr(row["STD_local"], "hour") else row["STD_local"]
    return f"{int(row['Flight Num'])}_{pd.Timestamp(row['Flight Date']).strftime('%Y%m%d')}T{t.strftime('%H%M%S')}"


def validate_existing(report: pd.DataFrame, sched: pd.DataFrame, rules: Rules, start=None, end=None):
    """Return (pairing_status_df, violations_df, unmatched_df). Pairings partly outside [start,end] are skipped & counted."""
    lookup = {f["flight_id"]: f for f in sched.to_dict("records")}
    rows, viol, unmatched, skipped = [], [], [], 0
    for pid, g, splits in pairing_groups(report):
        if start is not None and not g["Flight Date"].between(start, end).all():
            if g["Flight Date"].between(start, end).any():
                skipped += 1
            continue
        keys = [report_flight_id(r) for _, r in g.iterrows()]
        seq = [lookup.get(k) for k in keys]
        if any(x is None for x in seq):
            for k, x in zip(keys, seq):
                if x is None:
                    unmatched.append({"Pairing ID": pid, "Flight ID": k})
            rows.append({"Pairing ID": pid, "Status": "UNMATCHED", "Violations": None, "Flights": len(keys)})
            continue
        v = check_pairing(pid, seq, splits, rules)
        viol += [x.as_dict() for x in v]
        rows.append({"Pairing ID": pid, "Status": "INVALID" if v else "VALID", "Violations": len(v), "Flights": len(keys)})
    return pd.DataFrame(rows), pd.DataFrame(viol), pd.DataFrame(unmatched), skipped