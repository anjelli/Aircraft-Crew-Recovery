from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from crew_recovery.recovery_scenario import (
    RecoveryScenario,
    apply_flight_events,
    apply_flight_retiming_events,
    load_recovery_scenario,
)


class _FixedMGTResolver:
    def resolve(self, equipment, destination, arrival_country, arrival_date):
        # The example uses the approved route values for the acceptance case:
        # SIN turnaround 50 minutes; IPH turnaround 45 minutes.
        return (50 if destination == "SIN" else 45), "RESOLVED"


def _flight(fid, origin, destination, std, sta, mgt):
    return {
        "Day": pd.Timestamp("2026-10-25"),
        "STD_local": pd.Timestamp(std),
        "STA_local": pd.Timestamp(sta),
        "STD": pd.Timestamp(std),
        "STA": pd.Timestamp(sta),
        "Eqp": "320",
        "From": origin,
        "To": destination,
        "Arvl Ctry": "SG" if destination == "SIN" else "MY",
        "flight_id": fid,
        "mgt_min": mgt,
        "mgt_status": "RESOLVED",
    }


def test_485_delay_retimes_both_downstream_pairing_sectors_to_legal_mgt():
    """Acceptance case: a 20-minute delay to 485 requires +5 on 486 and 487."""
    baseline = pd.DataFrame([
        _flight("484_20261025T111500", "SIN", "IPH", "2026-10-25 11:15", "2026-10-25 12:30", 45),
        _flight("485_20261025T132000", "IPH", "SIN", "2026-10-25 13:20", "2026-10-25 14:40", 50),
        _flight("486_20261025T154500", "SIN", "IPH", "2026-10-25 15:45", "2026-10-25 17:05", 45),
        _flight("487_20261025T175000", "IPH", "SIN", "2026-10-25 17:50", "2026-10-25 19:10", 50),
    ]).set_index("flight_id", drop=False)
    schedule = baseline.reset_index(drop=True).copy()
    resolver = _FixedMGTResolver()

    delay = RecoveryScenario(
        schema_version=1,
        scenario_id="acceptance_delay_485",
        description="Delay flight 485 by 20 minutes.",
        events=tuple(load_recovery_scenario_from_dict({
            "schema_version": 1,
            "scenario_id": "acceptance_delay_485",
            "events": [{
                "event_id": "delay-485",
                "type": "FLIGHT_DELAY",
                "flight_id": "485_20261025T132000",
                "delay_minutes": 20,
            }],
        }).events),
    )
    disrupted, _ = apply_flight_events(
        schedule, delay, resolver, start_date="2026-10-25", end_date="2027-03-27"
    )

    retiming = load_recovery_scenario_from_dict({
        "schema_version": 1,
        "scenario_id": "acceptance_retime_486_487",
        "events": [
            {"event_id": "retime-486", "type": "FLIGHT_RETIME", "flight_id": "486_20261025T154500", "offset_minutes": 5},
            {"event_id": "retime-487", "type": "FLIGHT_RETIME", "flight_id": "487_20261025T175000", "offset_minutes": 5},
        ],
    })
    recovered, applied = apply_flight_retiming_events(
        disrupted,
        retiming,
        resolver,
        start_date="2026-10-25",
        end_date="2027-03-27",
        baseline_values=baseline,
    )
    by_id = recovered.set_index("flight_id")
    assert applied["retimed_flight_ids"] == ["486_20261025T154500", "487_20261025T175000"]

    gap_485_486 = (by_id.loc["486_20261025T154500", "STD"] - by_id.loc["485_20261025T132000", "STA"]).total_seconds() / 60
    gap_486_487 = (by_id.loc["487_20261025T175000", "STD"] - by_id.loc["486_20261025T154500", "STA"]).total_seconds() / 60
    assert gap_485_486 == 50
    assert gap_486_487 == 45
    assert gap_485_486 >= by_id.loc["485_20261025T132000", "mgt_min"]
    assert gap_486_487 >= by_id.loc["486_20261025T154500", "mgt_min"]


def load_recovery_scenario_from_dict(raw):
    """Use the production manifest validator for in-memory acceptance fixtures."""
    import json
    from pathlib import Path
    from tempfile import NamedTemporaryFile

    with NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
        json.dump(raw, handle)
        path = Path(handle.name)
    try:
        return load_recovery_scenario(path)
    finally:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# End-to-end regression: flights 485/486/487 through the real recovery runner.
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parents[1]
_RUNNER = _ROOT / "scripts" / "run_one_week_joint_recovery.py"
_FLIGHTS = {
    "485": "485_20261025T132000",
    "486": "486_20261025T154500",
    "487": "487_20261025T175000",
}
_DELAY_485 = {
    "event_id": "delay-flight-485", "type": "FLIGHT_DELAY",
    "flight_id": _FLIGHTS["485"], "delay_minutes": 20,
}


def _run_recovery(tmp_path, name, events):
    scenario = tmp_path / f"{name}.json"
    scenario.write_text(json.dumps({
        "schema_version": 1, "scenario_id": f"regression_{name}", "events": events,
    }))
    out = tmp_path / name
    proc = subprocess.run(
        [sys.executable, str(_RUNNER), "--start-date", "2026-10-25", "--days", "1",
         "--time-limit-seconds", "60", "--skip-pricing",
         "--scenario-file", str(scenario), "--output-dir", str(out)],
        cwd=_ROOT, text=True, capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    decisions = pd.read_csv(out / "flight_decisions.csv").set_index("flight_id", drop=False)
    return out, decisions


@pytest.fixture(scope="module")
def recoveries(tmp_path_factory):
    if not (_ROOT / "input" / "schedule.xlsx").is_file():
        pytest.skip("input/schedule.xlsx not available")
    tmp = tmp_path_factory.mktemp("recovery_485_487")
    retimes = [
        {"event_id": f"retime-{n}", "type": "FLIGHT_RETIME", "flight_id": _FLIGHTS[n], "offset_minutes": 5}
        for n in ("486", "487")
    ]
    return {
        "delay_only": _run_recovery(tmp, "delay_only", [_DELAY_485]),
        "retimed": _run_recovery(tmp, "retimed", [_DELAY_485, *retimes]),
    }


def test_485_486_487_delay_only_is_not_recoverable_without_retiming(recoveries):
    """Control: the failure is real, so a passing retimed run cannot be vacuous."""
    _, decisions = recoveries["delay_only"]
    for fid in _FLIGHTS.values():
        assert decisions.loc[fid, "recovery_action"] == "CANCEL"
        assert decisions.loc[fid, "cancellation_reason_code"] == "NO_VALID_PAIRING_CANDIDATE"


def test_485_486_487_retimed_recovery_operates_with_valid_crew_and_aircraft(recoveries):
    out, d = recoveries["retimed"]
    ts = pd.to_datetime
    expected_shift = {"485": 20, "486": 5, "487": 5}
    for num, fid in _FLIGHTS.items():
        row = d.loc[fid]
        # The flights are actually operated, not merely retimed on paper.
        assert row["recovery_action"] == "OPERATE", row["cancellation_reason_code"]
        # Adjusted times: original + shift, duration preserved, equipment unchanged.
        shift = pd.Timedelta(minutes=expected_shift[num])
        assert ts(row["recovered_STD_local"]) == ts(row["original_STD_local"]) + shift
        duration = ts(row["original_STA_local"]) - ts(row["original_STD_local"])
        assert ts(row["recovered_STA_local"]) - ts(row["recovered_STD_local"]) == duration
        assert row["recovered_Eqp"] == row["baseline_Eqp"] == "320"
    assert d.loc[_FLIGHTS["485"], "retime_minutes"] == 0  # disruption, not a recovery retime
    assert d.loc[_FLIGHTS["486"], "retime_minutes"] == 5
    assert d.loc[_FLIGHTS["487"], "retime_minutes"] == 5

    # Crew: one validated pairing covers all three flights; independent validator says VALID.
    pairing_ids = {d.loc[f, "assigned_pairing_id"] for f in _FLIGHTS.values()}
    assert len(pairing_ids) == 1 and next(iter(pairing_ids)) == "PAIR_11"
    status = pd.read_csv(out / "pairing_validation_status.csv")
    pairing_row = status[status["Pairing ID"].astype(str) == "11"].iloc[0]
    assert pairing_row["Status"] == "VALID" and pairing_row["Violations"] == 0
    selected = pd.read_csv(out / "selected_pairings.csv")
    covered = set(selected.loc[selected["pairing_id"] == "PAIR_11", "flight_id"])
    assert set(_FLIGHTS.values()) <= covered

    # Crew connections inside the pairing, recomputed here from the exported times.
    rot = pd.read_csv(out / "aircraft_rotations.csv").set_index("flight_id", drop=False)
    seq = selected[selected["pairing_id"] == "PAIR_11"].sort_values("sequence")["flight_id"].tolist()
    for prev, nxt in zip(seq, seq[1:]):
        gap = (ts(rot.loc[nxt, "recovered_STD_local"]) - ts(rot.loc[prev, "recovered_STA_local"])).total_seconds() / 60
        assert rot.loc[prev, "To"] == rot.loc[nxt, "From"]
        assert gap >= rot.loc[prev, "mgt_min"], (prev, nxt, gap)

    # Aircraft: station continuity and MGT on both sides of every affected flight.
    operated = rot[rot["recovery_action"] == "OPERATE"]
    for fid in _FLIGHTS.values():
        tail = operated.loc[fid, "assigned_aircraft_tail"]
        line = operated[operated["assigned_aircraft_tail"] == tail].copy()
        line["_std"] = ts(line["recovered_STD_local"])
        line = line.sort_values("_std").reset_index(drop=True)
        i = int(line.index[line["flight_id"] == fid][0])
        for a, b in ((i - 1, i), (i, i + 1)):
            if a < 0 or b >= len(line):
                continue
            prev, nxt = line.loc[a], line.loc[b]
            gap = (ts(nxt["recovered_STD_local"]) - ts(prev["recovered_STA_local"])).total_seconds() / 60
            assert prev["To"] == nxt["From"], (prev["flight_id"], nxt["flight_id"])
            assert gap >= prev["mgt_min"], (prev["flight_id"], nxt["flight_id"], gap)
    violations = out / "aircraft_validation_violations.csv"
    if violations.read_text().strip():
        assert not pd.read_csv(violations)["flight_id"].isin(_FLIGHTS.values()).any()

    # Recovery strictly improves on the delay-only control for these flights.
    _, control = recoveries["delay_only"]
    for fid in _FLIGHTS.values():
        assert control.loc[fid, "recovery_action"] == "CANCEL"
