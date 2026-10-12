"""Independent exhaustive checks for the bounded resource-constrained pricing domain.

The reference enumerator below deliberately does not call ResourceConstrainedPairingPricer
or its duty-enumeration helpers. It brute-forces flight permutations on tiny schedules,
applies the documented connection/FDP limits, and validates completed pairings with
the independent legality validator. Keep instances tiny: factorial enumeration is the point.
"""
from __future__ import annotations

from datetime import timedelta
from itertools import permutations
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import independent_validator as iv
from column_generation import reduced_cost
from crew_recovery.resource_constrained_pricing import ResourceConstrainedPairingPricer


HUB = "SIN"
LAYOVERS = ("KUL", "BKK")
MAX_SECTORS = 4


def flight(fid, frm, to, std, *, equipment="320", mgt=50.0):
    from datetime import datetime

    departure = datetime.fromisoformat(std)
    return {
        "flight_id": fid,
        "From": frm,
        "To": to,
        "STD": departure,
        "STA": departure + timedelta(hours=1),
        "STD_local": departure,
        "STA_local": departure + timedelta(hours=1),
        "Eqp": equipment,
        "mgt_min": mgt,
        "mgt_status": "TEST",
        "active": True,
    }


def fixture_rules():
    return iv.Rules(
        fdp={band: [16.0] * 8 for band in (
            "0600-0759", "0800-1459", "1500-2159", "2200-0559"
        )},
        dsb=0,
        feb=0,
        deb=0,
        max_layover_h=72.0,
    )


def make_pricer(flights):
    rules = fixture_rules()

    def validate(seq, splits, pairing_type):
        return iv.check_pairing("reference-test", list(seq), [list(s) for s in splits], rules)

    pricer = ResourceConstrainedPairingPricer(
        flights,
        fdp_limits=rules.fdp,
        duty_start_buffer=rules.dsb,
        fdp_end_buffer=rules.feb,
        duty_end_buffer=rules.deb,
        max_layover_hours=rules.max_layover_h,
        layover_stations=LAYOVERS,
        validator=validate,
        pairing_cost=1.0,
        hub=HUB,
        max_sectors_per_duty=MAX_SECTORS,
    )
    return pricer, rules, validate


def _reference_duty_feasible(path, rules):
    """Independent small-instance feasibility check, not production-pricer code."""
    if not path or len(path) > MAX_SECTORS:
        return False
    start = path[0]["STD"] - timedelta(minutes=rules.dsb)
    end_buffer = rules.deb if len(path) >= MAX_SECTORS else rules.feb
    end = path[-1]["STA"] + timedelta(minutes=end_buffer)
    hours = (end - start).total_seconds() / 3600
    band = iv.fdp_band(path[0]["STD_local"] - timedelta(minutes=rules.dsb))
    limits = rules.fdp.get(band)
    if limits is None or len(limits) < len(path):
        return False
    return hours <= limits[len(path) - 1] + 1e-9


def _reference_connection_feasible(path):
    """Check airport continuity, MGT, and the documented equipment-change buffer."""
    for index, (previous, nxt) in enumerate(zip(path, path[1:])):
        if str(previous["To"]).upper() != str(nxt["From"]).upper():
            return False
        mgt = previous.get("mgt_min")
        if mgt is None:
            return False
        mgt = float(mgt)
        # The source pairing rules use a 90-minute buffer for an equipment change
        # after the second sector in a duty.
        if index == 1 and str(previous.get("Eqp", "")) != str(nxt.get("Eqp", "")):
            mgt = 90.0
        if nxt["STD"] < previous["STA"] + timedelta(minutes=mgt):
            return False
    return True


def exhaustive_reference_columns(flights, rules, validate):
    """Brute-force every pairing in the declared domain for a tiny schedule."""
    active = [f for f in flights if f.get("active", True)]
    allowed_starts = {HUB, *LAYOVERS}
    duties = {}

    for length in range(1, min(MAX_SECTORS, len(active)) + 1):
        for path in permutations(active, length):
            if str(path[0]["From"]).upper() not in allowed_starts:
                continue
            if not _reference_connection_feasible(path):
                continue
            if not _reference_duty_feasible(path, rules):
                continue
            ids = tuple(str(f["flight_id"]) for f in path)
            duties[ids] = path

    reference = {}

    def admit(path, splits, pairing_type):
        ids = tuple(str(f["flight_id"]) for f in path)
        if len(set(ids)) != len(ids):
            return
        if validate(path, splits, pairing_type):
            return
        # Compare semantic identity, not the production-generated hash.
        key = (pairing_type, ids, tuple(tuple(s) for s in splits))
        reference[key] = 1.0

    for ids, duty in duties.items():
        if str(duty[0]["From"]).upper() == HUB and str(duty[-1]["To"]).upper() == HUB and len(ids) >= 2:
            admit(duty, [list(range(len(ids)))], "single_day")

    outbound = {}
    inbound = {}
    for ids, duty in duties.items():
        start = str(duty[0]["From"]).upper()
        end = str(duty[-1]["To"]).upper()
        if start == HUB and end in LAYOVERS:
            outbound.setdefault(end, []).append(duty)
        if start in LAYOVERS and end == HUB:
            inbound.setdefault(start, []).append(duty)

    for station in sorted(set(outbound) & set(inbound)):
        for first in outbound[station]:
            for second in inbound[station]:
                first_ids = {str(f["flight_id"]) for f in first}
                second_ids = {str(f["flight_id"]) for f in second}
                if first_ids & second_ids or second[0]["STD"] <= first[-1]["STA"]:
                    continue
                if second[0]["STD"] - first[-1]["STA"] > timedelta(hours=rules.max_layover_h):
                    continue
                path = tuple(first) + tuple(second)
                split_at = len(first)
                admit(path, [list(range(split_at)), list(range(split_at, len(path)))], "multi_day")

    return reference


def production_semantic_columns(pricer, flight_ids):
    # Very large duals make every legal nonempty candidate in this tiny universe improving.
    batch = pricer({fid: 1000.0 for fid in flight_ids}, frozenset(), 1e-7, 100_000)
    assert not batch.certified_no_improving_column
    return {
        (column.pairing_type, tuple(column.flight_ids), tuple(
            tuple(range(0)) for _ in ()
        )): column
        for column in batch.columns
    }, batch


def test_pricer_matches_independent_exhaustive_pairing_universe():
    flights = [
        flight("S-K", "SIN", "KUL", "2026-10-25T09:00"),
        flight("S-B", "SIN", "BKK", "2026-10-25T10:00"),
        flight("K-S", "KUL", "SIN", "2026-10-26T08:00"),
        flight("B-S", "BKK", "SIN", "2026-10-26T09:00"),
        # A second leg creates alternative duty paths; validator rules still decide legality.
        flight("K-B", "KUL", "BKK", "2026-10-25T12:00"),
        # An intentionally disconnected flight must never enter a candidate.
        flight("X-Y", "HKG", "TPE", "2026-10-25T11:00"),
    ]
    pricer, rules, validate = make_pricer(flights)
    reference = exhaustive_reference_columns(flights, rules, validate)

    batch = pricer(
        {f["flight_id"]: 1000.0 for f in flights},
        frozenset(),
        1e-7,
        100_000,
    )
    observed = {}
    for column in batch.columns:
        ids = tuple(column.flight_ids)
        if column.pairing_type == "single_day":
            splits = (tuple(range(len(ids))),)
        else:
            # The independent validator has already checked the actual split; recover
            # the only supported two-duty split by matching the station transition.
            split_at = next(
                i for i in range(1, len(ids))
                if next(f for f in flights if f["flight_id"] == ids[i - 1])["To"]
                != next(f for f in flights if f["flight_id"] == ids[i])["To"]
                or next(f for f in flights if f["flight_id"] == ids[i])["From"] in LAYOVERS
            )
            splits = (tuple(range(split_at)), tuple(range(split_at, len(ids))))
        observed[(column.pairing_type, ids, splits)] = column.cost

    assert set(observed) == set(reference)
    assert observed == reference
    assert all("X-Y" not in key[1] for key in observed)


@pytest.mark.parametrize(
    ("duals", "expected_sign"),
    [
        ({"OUT": 2.0, "BACK": 2.0}, "negative"),
        ({"OUT": 0.5, "BACK": 0.5}, "zero"),
        ({"OUT": 0.0, "BACK": 0.0}, "positive"),
    ],
)
def test_reduced_cost_signs_are_reported_consistently(duals, expected_sign):
    flights = [
        flight("OUT", "SIN", "KUL", "2026-10-25T09:00"),
        flight("BACK", "KUL", "SIN", "2026-10-26T08:00"),
    ]
    pricer, _, _ = make_pricer(flights)
    batch = pricer(duals, frozenset(), 1e-7, 100)
    assert batch.columns
    costs = [reduced_cost(column, duals) for column in batch.columns]
    best = min(costs)
    if expected_sign == "negative":
        assert best < -1e-7
        assert not batch.certified_no_improving_column
    elif expected_sign == "zero":
        assert best == pytest.approx(0.0)
        assert batch.certified_no_improving_column
    else:
        assert best > 0.0
        assert batch.certified_no_improving_column


def test_certification_means_no_unseen_improving_column_when_all_are_active():
    flights = [
        flight("OUT", "SIN", "KUL", "2026-10-25T09:00"),
        flight("BACK", "KUL", "SIN", "2026-10-26T08:00"),
    ]
    pricer, _, _ = make_pricer(flights)
    improving = pricer({"OUT": 100.0, "BACK": 100.0}, frozenset(), 1e-7, 100)
    assert improving.columns
    active_ids = frozenset(column.column_id for column in improving.columns)

    # Use the same duals deliberately: every improving column is already active.
    second = pricer({"OUT": 100.0, "BACK": 100.0}, active_ids, 1e-7, 100)
    assert second.columns == ()
    assert second.certified_no_improving_column
