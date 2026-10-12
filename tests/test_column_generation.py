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
