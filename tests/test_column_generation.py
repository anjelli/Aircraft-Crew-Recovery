from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from column_generation import (  # noqa: E402
    PairingColumn,
    finite_pool_price,
    reduced_cost,
    run_finite_pool_column_generation,
    solve_restricted_master,
)


def toy_problem():
    flights = ["A", "B", "C"]
    columns = [
        PairingColumn("A", ("A",)),
        PairingColumn("B", ("B",)),
        PairingColumn("C", ("C",)),
        PairingColumn("AB", ("A", "B")),
        PairingColumn("BC", ("B", "C")),
    ]
    return flights, columns


def test_reduced_cost_uses_flight_duals():
    col = PairingColumn("AB", ("A", "B"), cost=1.0)
    assert reduced_cost(col, {"A": 0.8, "B": 0.5}) == pytest.approx(-0.3)


def test_finite_pool_column_generation_matches_full_pool_lp():
    flights, universe = toy_problem()
    penalty = len(flights) + 1
    seeded = [c for c in universe if len(c.flight_ids) == 1]

    cg = run_finite_pool_column_generation(
        flights,
        universe,
        initial_columns=seeded,
        uncovered_penalty=penalty,
        batch_size=1,
    )
    full_pool = solve_restricted_master(flights, universe, penalty)

    assert cg.converged
    assert cg.stop_reason == "no_negative_reduced_cost_columns"
    assert cg.solution.objective == pytest.approx(full_pool.objective, abs=1e-8)
    assert {c.column_id for c in cg.columns} & {"AB", "BC"}
    assert sum(cg.solution.uncovered_values.values()) == pytest.approx(0.0, abs=1e-8)
    assert cg.best_reduced_cost is None or cg.best_reduced_cost >= -1e-7


def test_empty_candidate_universe_is_feasible_with_uncovered_slack():
    flights = ["A", "B", "C"]
    result = run_finite_pool_column_generation(flights, [])

    assert result.converged
    assert result.iterations == 1
    assert result.solution.objective == pytest.approx(12.0)
    assert all(v == pytest.approx(1.0) for v in result.solution.uncovered_values.values())


def test_iteration_limit_is_reported_when_improving_columns_remain():
    flights, universe = toy_problem()
    seeded = [c for c in universe if len(c.flight_ids) == 1]

    result = run_finite_pool_column_generation(
        flights, universe, initial_columns=seeded, max_iterations=1
    )

    assert not result.converged
    assert result.stop_reason == "iteration_limit_with_improving_columns"
    assert result.best_reduced_cost is not None
    assert result.best_reduced_cost < 0


def test_non_unit_cost_requires_explicit_uncovered_penalty():
    flights = ["A", "B"]
    columns = [PairingColumn("AB", ("A", "B"), cost=2.5)]

    with pytest.raises(ValueError, match="Provide uncovered_penalty"):
        run_finite_pool_column_generation(flights, columns)


def test_column_cannot_repeat_flight_or_reference_unknown_flight():
    with pytest.raises(ValueError, match="repeats a flight ID"):
        PairingColumn("bad", ("A", "A"))

    with pytest.raises(ValueError, match="inactive/unknown flights"):
        solve_restricted_master(["A"], [PairingColumn("bad", ("B",))], 2.0)


def test_duplicate_candidate_ids_rejected():
    flights = ["A"]
    universe = [PairingColumn("same", ("A",)), PairingColumn("same", ("A",))]
    with pytest.raises(ValueError, match="duplicate column IDs"):
        run_finite_pool_column_generation(flights, universe)


def test_pricing_excludes_active_columns_and_sorts_by_reduced_cost():
    flights, universe = toy_problem()
    priced = finite_pool_price(
        universe,
        {"A", "B", "C"},
        {"A": 2.0, "B": 2.0, "C": 2.0},
        batch_size=2,
    )
    assert len(priced) == 2
    assert priced[0][1] <= priced[1][1]
    assert all(column.column_id not in {"A", "B", "C"} for column, _ in priced)
    assert all(math.isfinite(rc) for _, rc in priced)



def test_multiday_pairing_column_is_added_when_its_reduced_cost_is_improving():
    # The IDs represent a legal overnight pairing already emitted by the
    # pairing-generation/validation layer; legality is not inferred here.
    flights = ["OUTBOUND", "RETURN"]
    overnight = PairingColumn("overnight_pairing_17", ("OUTBOUND", "RETURN"), cost=1.0)
    initial = [
        PairingColumn("day_duty_out", ("OUTBOUND",), cost=1.0),
        PairingColumn("day_duty_return", ("RETURN",), cost=1.0),
    ]

    result = run_finite_pool_column_generation(
        flights,
        [*initial, overnight],
        initial_columns=initial,
        uncovered_penalty=100.0,
        batch_size=1,
    )

    assert result.converged
    assert any(c.column_id == "overnight_pairing_17" for c in result.columns)
    assert result.solution.objective == pytest.approx(1.0)
    assert result.solution.uncovered_values["OUTBOUND"] == pytest.approx(0.0)
    assert result.solution.uncovered_values["RETURN"] == pytest.approx(0.0)



def _schedule_flight(fid, frm, to, std):
    from datetime import datetime, timedelta
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
        "mgt_min": 50,
        "mgt_status": "TEST_RULE",
        "active": True,
    }


def test_builder_pairing_adapter_preserves_and_validates_multiday_columns():
    import independent_validator as iv
    from crew_recovery.pairing_columns import (
        build_pairing_columns,
        independent_pairing_validator,
    )

    rules = iv.Rules(fdp=iv.load_fdp(ROOT / "originals" / "fdp_rules.csv"))
    outbound_1 = _schedule_flight("OUT1", "SIN", "KUL", "2026-10-25T09:00")
    inbound_1 = _schedule_flight("BACK1", "KUL", "SIN", "2026-10-26T08:00")
    outbound_2 = _schedule_flight("OUT2", "SIN", "BKK", "2026-10-25T10:00")
    inbound_2 = _schedule_flight("BACK2", "BKK", "SIN", "2026-10-26T09:00")

    pairings = [[outbound_1, inbound_1], [outbound_2, inbound_2]]
    metadata = [
        {"pairing_type": "multi_day", "num_sectors": 2, "num_duties": 2,
         "is_multi_day": True, "layover_station": "KUL", "layover_hours": 22.0},
        {"pairing_type": "multi_day", "num_sectors": 2, "num_duties": 2,
         "is_multi_day": True, "layover_station": "BKK", "layover_hours": 22.0},
    ]
    built = build_pairing_columns(
        pairings,
        metadata,
        ["OUT1", "BACK1", "OUT2", "BACK2"],
        validator=independent_pairing_validator(rules),
    )

    assert not built.rejected
    assert len(built.columns) == 2
    assert all(column.is_multi_day for column in built.columns)
    assert all(column.num_duties == 2 for column in built.columns)
    assert all(column.pairing_type == "multi_day" for column in built.columns)


def test_adapter_rejects_multiday_candidate_that_fails_independent_rest_check():
    import independent_validator as iv
    from crew_recovery.pairing_columns import (
        build_pairing_columns,
        independent_pairing_validator,
    )

    rules = iv.Rules(fdp=iv.load_fdp(ROOT / "originals" / "fdp_rules.csv"))
    outbound = _schedule_flight("OUT", "SIN", "KUL", "2026-10-25T09:00")
    too_early_return = _schedule_flight("BACK", "KUL", "SIN", "2026-10-25T13:00")
    built = build_pairing_columns(
        [[outbound, too_early_return]],
        [{"pairing_type": "multi_day", "num_sectors": 2, "num_duties": 2,
          "is_multi_day": True, "layover_station": "KUL", "layover_hours": 3.0}],
        ["OUT", "BACK"],
        validator=independent_pairing_validator(rules),
    )

    assert built.columns == ()
    assert len(built.rejected) == 1
    assert "independent_validation_failed" in built.rejected[0].reason


def test_validated_multiday_columns_match_exhaustive_full_pool_lp():
    import independent_validator as iv
    from crew_recovery.pairing_columns import (
        build_pairing_columns,
        independent_pairing_validator,
    )

    rules = iv.Rules(fdp=iv.load_fdp(ROOT / "originals" / "fdp_rules.csv"))
    a_out = _schedule_flight("A_OUT", "SIN", "KUL", "2026-10-25T09:00")
    a_back = _schedule_flight("A_BACK", "KUL", "SIN", "2026-10-26T08:00")
    b_out = _schedule_flight("B_OUT", "SIN", "BKK", "2026-10-25T10:00")
    b_back = _schedule_flight("B_BACK", "BKK", "SIN", "2026-10-26T09:00")
    pairings = [[a_out, a_back], [b_out, b_back]]
    metadata = [
        {"pairing_type": "multi_day", "num_sectors": 2, "num_duties": 2,
         "is_multi_day": True, "layover_station": "KUL", "layover_hours": 22.0},
        {"pairing_type": "multi_day", "num_sectors": 2, "num_duties": 2,
         "is_multi_day": True, "layover_station": "BKK", "layover_hours": 22.0},
    ]
    built = build_pairing_columns(
        pairings, metadata, ["A_OUT", "A_BACK", "B_OUT", "B_BACK"],
        validator=independent_pairing_validator(rules),
    )
    assert not built.rejected
    universe = list(built.columns)
    flights = ["A_OUT", "A_BACK", "B_OUT", "B_BACK"]
    seed = [universe[0]]
    penalty = len(flights) + 1

    cg = run_finite_pool_column_generation(
        flights, universe, initial_columns=seed, uncovered_penalty=penalty, batch_size=1
    )
    exhaustive = solve_restricted_master(flights, universe, penalty)

    assert cg.converged
    assert cg.solution.objective == pytest.approx(exhaustive.objective, abs=1e-8)
    assert {c.column_id for c in cg.columns} == {c.column_id for c in universe}
    assert all(v == pytest.approx(0.0, abs=1e-8) for v in cg.solution.uncovered_values.values())
