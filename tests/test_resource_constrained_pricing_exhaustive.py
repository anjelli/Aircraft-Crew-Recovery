"""Independent exhaustive checks for the bounded resource-constrained pricing domain.

The reference enumerator deliberately does not call ResourceConstrainedPairingPricer
or its duty-enumeration helpers. It brute-forces flight permutations on tiny schedules,
applies the documented connection/FDP limits, and validates complete pairings with
the independent legality validator. Keep instances tiny: factorial enumeration is the point.
"""
from __future__ import annotations

from datetime import datetime, timedelta
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
    """Independent small-instance FDP check, not production-pricer code."""
    if not path or len(path) > MAX_SECTORS:
        return False
    start = path[0]["STD"] - timedelta(minutes=rules.dsb)
    end_buffer = rules.deb if len(path) >= MAX_SECTORS else rules.feb
    end = path[-1]["STA"] + timedelta(minutes=end_buffer)
    hours = (end - start).total_seconds() / 3600
    band = iv.fdp_band(path[0]["STD_local"] - timedelta(minutes=rules.dsb))
    limits = rules.fdp.get(band)
    return limits is not None and len(limits) >= len(path) and hours <= limits[len(path) - 1] + 1e-9


def _reference_connection_feasible(path):
    """Check airport continuity, MGT, and the documented equipment-change buffer."""
    for index, (previous, nxt) in enumerate(zip(path, path[1:])):
        if str(previous["To"]).upper() != str(nxt["From"]).upper():
            return False
        mgt = previous.get("mgt_min")
        if mgt is None:
            return False
        mgt = float(mgt)
        if index == 1 and str(previous.get("Eqp", "")) != str(nxt.get("Eqp", "")):
            mgt = 90.0
        if nxt["STD"] < previous["STA"] + timedelta(minutes=mgt):
            return False
    return True


def exhaustive_reference_columns(flights, rules, validate):
    """Brute-force all legal single-duty and two-duty pairings in the declared domain."""
    active = [f for f in flights if f.get("active", True)]
    allowed_starts = {HUB, *LAYOVERS}
    duties = {}

    for length in range(1, min(MAX_SECTORS, len(active)) + 1):
        for path in permutations(active, length):
            if str(path[0]["From"]).upper() not in allowed_starts:
                continue
            if not _reference_connection_feasible(path) or not _reference_duty_feasible(path, rules):
                continue
            duties[tuple(str(f["flight_id"]) for f in path)] = path

    reference = {}

    def admit(path, splits, pairing_type):
        ids = tuple(str(f["flight_id"]) for f in path)
        if len(set(ids)) != len(ids) or validate(path, splits, pairing_type):
            return
        key = (pairing_type, ids, tuple(tuple(s) for s in splits))
        reference[key] = 1.0

    for ids, duty in duties.items():
        if str(duty[0]["From"]).upper() == HUB and str(duty[-1]["To"]).upper() == HUB and len(ids) >= 2:
            admit(duty, [list(range(len(ids)))], "single_day")

    outbound, inbound = {}, {}
    for duty in duties.values():
        start, end = str(duty[0]["From"]).upper(), str(duty[-1]["To"]).upper()
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


def test_pricer_matches_independent_exhaustive_pairing_universe():
    flights = [
        flight("S-K", "SIN", "KUL", "2026-10-25T09:00"),
        flight("S-B", "SIN", "BKK", "2026-10-25T10:00"),
        flight("K-S-DAY", "KUL", "SIN", "2026-10-25T11:30"),
        flight("K-S", "KUL", "SIN", "2026-10-26T08:00"),
        flight("B-S", "BKK", "SIN", "2026-10-26T09:00"),
        flight("K-B", "KUL", "BKK", "2026-10-25T12:00"),
        flight("X-Y", "HKG", "TPE", "2026-10-25T11:00"),
    ]
    pricer, rules, validate = make_pricer(flights)
    reference = exhaustive_reference_columns(flights, rules, validate)
    assert reference, "The test schedule must contain legal single- and multi-day candidates."

    batch = pricer({f["flight_id"]: 1000.0 for f in flights}, frozenset(), 1e-7, 100_000)
    observed_pairs = {(c.pairing_type, tuple(c.flight_ids)) for c in batch.columns}
    reference_pairs = {(kind, ids) for kind, ids, _splits in reference}
    assert observed_pairs == reference_pairs
    assert all("X-Y" not in ids for _kind, ids in observed_pairs)

    # Every reference sequence has one unambiguous split contract and the expected duty count.
    assert len(reference_pairs) == len(reference)
    for kind, ids, splits in reference:
        assert len(splits) == (2 if kind == "multi_day" else 1)
        assert all(splits)
        assert tuple(i for split in splits for i in split) == tuple(range(len(ids)))


@pytest.mark.parametrize(
    ("duals", "expected_sign"),
    [
        ({"OUT": 2.0, "BACK": 2.0}, "negative"),
        ({"OUT": 0.5, "BACK": 0.5}, "zero"),
        ({"OUT": 0.0, "BACK": 0.0}, "positive"),
    ],
)
def test_reduced_cost_signs_and_pricing_decisions(duals, expected_sign):
    flights = [
        flight("OUT", "SIN", "KUL", "2026-10-25T09:00"),
        flight("BACK", "KUL", "SIN", "2026-10-26T08:00"),
    ]
    pricer, _, _ = make_pricer(flights)
    batch = pricer(duals, frozenset(), 1e-7, 100)

    if expected_sign == "negative":
        assert batch.columns
        assert min(reduced_cost(c, duals) for c in batch.columns) < -1e-7
        assert not batch.certified_no_improving_column
    else:
        assert batch.columns == ()
        assert batch.certified_no_improving_column
        assert batch.best_reduced_cost == pytest.approx(0.0 if expected_sign == "zero" else 1.0)


def test_certification_means_no_unseen_improving_column_when_all_are_active():
    flights = [
        flight("OUT", "SIN", "KUL", "2026-10-25T09:00"),
        flight("BACK", "KUL", "SIN", "2026-10-26T08:00"),
    ]
    pricer, _, _ = make_pricer(flights)
    duals = {"OUT": 100.0, "BACK": 100.0}
    first = pricer(duals, frozenset(), 1e-7, 100)
    assert first.columns
    active_ids = frozenset(column.column_id for column in first.columns)

    second = pricer(duals, active_ids, 1e-7, 100)
    assert second.columns == ()
    assert second.certified_no_improving_column
