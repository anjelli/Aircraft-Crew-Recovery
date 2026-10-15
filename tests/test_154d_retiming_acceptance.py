from __future__ import annotations

from datetime import datetime

import pandas as pd

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
