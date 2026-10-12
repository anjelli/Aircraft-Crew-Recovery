from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
sys.path.insert(0, str(ROOT))

import independent_validator as iv
from column_generation import solve_restricted_master
from crew_recovery.integrated_column_generation import solve_with_pricing
from crew_recovery.resource_constrained_pricing import ResourceConstrainedPairingPricer


def flight(fid, frm, to, std):
    departure = datetime.fromisoformat(std)
    arrival = departure + timedelta(hours=1)
    return {
        "flight_id": fid,
        "From": frm,
        "To": to,
        "STD": departure,
        "STA": arrival,
        "STD_local": departure,
        "STA_local": arrival,
        "Eqp": "320",
        "mgt_min": 50.0,
        "mgt_status": "TEST",
        "active": True,
    }


def make_pricer(flights):
    rules = iv.Rules(
        fdp={band: [16.0] * 8 for band in (
            "0600-0759", "0800-1459", "1500-2159", "2200-0559"
        )},
        dsb=0,
        feb=0,
        deb=0,
        max_layover_h=72.0,
    )

    def validate(seq, splits, pairing_type):
        return iv.check_pairing("priced", list(seq), [list(s) for s in splits], rules)

    return ResourceConstrainedPairingPricer(
        flights,
        fdp_limits=rules.fdp,
        duty_start_buffer=rules.dsb,
        fdp_end_buffer=rules.feb,
        duty_end_buffer=rules.deb,
        max_layover_hours=rules.max_layover_h,
        layover_stations=["KUL", "BKK"],
        validator=validate,
        pairing_cost=1.0,
        hub="SIN",
        max_sectors_per_duty=4,
    )


def test_exact_pricer_discovers_legal_overnight_pairing_and_converges():
    flights = [
        flight("OUT", "SIN", "KUL", "2026-10-25T09:00"),
        flight("BACK", "KUL", "SIN", "2026-10-26T08:00"),
    ]
    pricer = make_pricer(flights)

    # High duals expose the complete tiny pricing domain for the direct-LP check.
    exhaustive_batch = pricer(
        {"OUT": 100.0, "BACK": 100.0}, frozenset(), 1e-7, 100
    )
    universe = list(exhaustive_batch.columns)
    overnight = [c for c in universe if c.is_multi_day]
    assert len(overnight) == 1
    assert overnight[0].flight_ids == ("OUT", "BACK")
    assert overnight[0].num_duties == 2
    assert not exhaustive_batch.certified_no_improving_column

    result = solve_with_pricing(
        ["OUT", "BACK"],
        pricer,
        uncovered_penalty=3.0,
        batch_size=100,
        max_iterations=10,
    )
    full = solve_restricted_master(["OUT", "BACK"], universe, 3.0)

    assert result.converged
    assert result.stop_reason == "pricing_certified_no_improving_column"
    assert result.solution.objective == pytest.approx(full.objective)
    assert result.solution.objective == pytest.approx(1.0)
    assert all(value == pytest.approx(0.0) for value in result.solution.uncovered_values.values())


def test_exact_pricer_rejects_multi_day_pairing_with_insufficient_rest():
    flights = [
        flight("OUT", "SIN", "KUL", "2026-10-25T09:00"),
        flight("BACK", "KUL", "SIN", "2026-10-25T13:00"),
    ]
    pricer = make_pricer(flights)
    batch = pricer({"OUT": 100.0, "BACK": 100.0}, frozenset(), 1e-7, 100)

    assert not any(c.is_multi_day for c in batch.columns)
    assert batch.certified_no_improving_column


def test_pricer_does_not_claim_improvement_when_candidate_is_active():
    flights = [
        flight("OUT", "SIN", "KUL", "2026-10-25T09:00"),
        flight("BACK", "KUL", "SIN", "2026-10-26T08:00"),
    ]
    pricer = make_pricer(flights)
    first = pricer({"OUT": 100.0, "BACK": 100.0}, frozenset(), 1e-7, 100)
    active = frozenset(c.column_id for c in first.columns)
    second = pricer({"OUT": 0.5, "BACK": 0.5}, active, 1e-7, 100)

    assert second.columns == ()
    assert second.certified_no_improving_column
