from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))

from crew_recovery.resource_constrained_pricing import ResourceConstrainedPairingPricer
from run_one_week_joint_recovery import build_pairing_candidate_diagnostics
from test_resource_constrained_pricing import flight, make_pricer


def _flights():
    return [
        flight("OUT", "SIN", "KUL", "2026-10-25T09:00"),
        flight("BACK", "KUL", "SIN", "2026-10-26T08:00"),
        flight("OUT2", "SIN", "KUL", "2026-10-25T18:00"),
    ]


def test_pricer_counts_reconcile_and_split_by_type():
    pricer = make_pricer(_flights())
    pool = pricer.generate_candidates()
    d = pricer.candidate_diagnostics
    assert d["generated"] > 0
    assert d["generated"] == d["rejected_total"] + d["passed_validation"]
    assert d["passed_validation"] == d["deduplicated_within_pricer"] + d["unique_columns_returned"]
    assert d["unique_columns_returned"] == len(pool)
    assert sum(d["generated_by_type"].values()) == d["generated"]


def test_pricer_counts_validator_rejections():
    base = make_pricer(_flights())
    rejecting = ResourceConstrainedPairingPricer(
        _flights(),
        fdp_limits=base.fdp_limits, duty_start_buffer=0, fdp_end_buffer=0,
        duty_end_buffer=0, max_layover_hours=72.0, layover_stations=["KUL", "BKK"],
        validator=lambda seq, splits, pairing_type: ["always reject"],
        hub="SIN", max_sectors_per_duty=4,
    )
    assert rejecting.generate_candidates() == ()
    d = rejecting.candidate_diagnostics
    assert d["generated"] > 0
    assert d["rejected_validator_violation"] == d["generated"] == d["rejected_total"]
    assert d["passed_validation"] == 0 and d["unique_columns_returned"] == 0


def test_pricer_counts_reset_on_each_generation_call():
    pricer = make_pricer(_flights())
    pricer.generate_candidates()
    first = pricer.candidate_diagnostics["generated"]
    pricer.generate_candidates()
    assert pricer.candidate_diagnostics["generated"] == first


def test_runner_diagnostics_identity_holds_and_detects_mismatch():
    ok = build_pairing_candidate_diagnostics(
        pricing_skipped=False, existing_valid=100,
        pricer={"generated": 50, "rejected_total": 10, "passed_validation": 40,
                "deduplicated_within_pricer": 5, "unique_columns_returned": 35,
                "generated_by_type": {"multi_day": 50}},
        duplicate_of_existing=4, skipped_outside_active=1, accepted=30, final_pool_size=130,
    )
    assert ok["counts_reconcile"] is True
    assert ok["deduplicated"]["total"] == 9
    assert ok["accepted_into_pool"] == 30 and ok["mode"] == "expanded"
    bad = build_pairing_candidate_diagnostics(
        pricing_skipped=False, existing_valid=100,
        pricer={"generated": 50, "rejected_total": 10, "passed_validation": 40,
                "deduplicated_within_pricer": 5, "unique_columns_returned": 35},
        duplicate_of_existing=4, skipped_outside_active=1, accepted=29, final_pool_size=129,
    )
    assert bad["counts_reconcile"] is False


def test_runner_diagnostics_existing_only_mode_is_all_zero_but_reconciles():
    d = build_pairing_candidate_diagnostics(
        pricing_skipped=True, existing_valid=8433, pricer={},
        duplicate_of_existing=0, skipped_outside_active=0, accepted=0, final_pool_size=8433,
    )
    assert d["mode"] == "existing_only" and d["generated"] == 0 and d["counts_reconcile"] is True


def test_layover_window_matches_brute_force_filter_and_preserves_order():
    import random
    from crew_recovery.resource_constrained_pricing import _Duty

    rng = random.Random(7)
    base = datetime.fromisoformat("2026-10-25T00:00")
    duties = [
        _Duty(flights=(), ids=(f"D{i}",), start_station="KUL", end_station="SIN",
              start_time=base + timedelta(hours=rng.choice(range(0, 200)) + rng.choice([0, 0.5])),
              end_time=base)
        for i in range(300)
    ]
    pricer = make_pricer(_flights())
    index = pricer._index_by_start(duties)
    for after_h in (0, 13.5, 60, 150, 199.5, 400):
        after = base + timedelta(hours=after_h)
        expected = [d for d in duties if d.start_time > after and d.start_time - after <= pricer.max_layover]
        assert pricer._starting_within_layover(index, after) == expected


def test_duty_extension_pruning_keeps_all_long_duties():
    # A 4-sector duty that uses almost the whole FDP limit must still be enumerated.
    flights = [
        flight("A", "SIN", "KUL", "2026-10-25T06:00"),
        flight("B", "KUL", "SIN", "2026-10-25T08:30"),
        flight("C", "SIN", "KUL", "2026-10-25T11:00"),
        flight("D", "KUL", "SIN", "2026-10-25T13:30"),
        flight("LATE", "SIN", "KUL", "2026-10-26T20:00"),
    ]
    pricer = make_pricer(flights)
    ids = {d.ids for d in pricer._enumerate_duties()}
    assert ("A", "B", "C", "D") in ids
    assert not any("LATE" in d and len(d) > 1 for d in ids)