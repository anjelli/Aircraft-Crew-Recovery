from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from column_generation import PairingColumn
from crew_recovery.integer_recovery import FlightOption, solve_integer_recovery


def _tiny_instance():
    options = [
        FlightOption("A_ORIG", "A", "operate", 0, 0.0),
        FlightOption("A_PLUS_30", "A", "retime", 30, 2.0),
        FlightOption("B_ORIG", "B", "operate", 0, 0.0),
        FlightOption("C_ORIG", "C", "operate", 0, 0.0),
    ]
    columns = [
        PairingColumn("A-original", ("A_ORIG",), cost=8.0),
        PairingColumn("B-C", ("B_ORIG", "C_ORIG"), cost=1.0, pairing_type="multi_day", is_multi_day=True, num_duties=2),
        PairingColumn("A-retimed-B", ("A_PLUS_30", "B_ORIG"), cost=1.0),
        PairingColumn("C-single", ("C_ORIG",), cost=1.0),
    ]
    cancel_costs = {"A": 20.0, "B": 20.0, "C": 20.0}
    return options, columns, cancel_costs


def _exhaustive_reference(flights, options, columns, cancellation_costs):
    """Enumerate all column subsets independently for this deliberately tiny fixture."""
    option_by_id = {option.option_id: option for option in options}
    best = float("inf")
    for mask in range(1 << len(columns)):
        selected = [columns[j] for j in range(len(columns)) if mask & (1 << j)]
        assigned = {}
        feasible = True
        total = 0.0
        for column in selected:
            total += column.cost
            for option_id in column.flight_ids:
                option = option_by_id[option_id]
                if option.flight_id in assigned:
                    feasible = False
                    break
                assigned[option.flight_id] = option
                total += option.action_cost
            if not feasible:
                break
        if not feasible:
            continue
        total += sum(cancellation_costs[f] for f in flights if f not in assigned)
        best = min(best, total)
    return best


def test_integer_recovery_matches_exhaustive_tiny_instance_and_selects_retime():
    options, columns, cancel_costs = _tiny_instance()
    flights = ["A", "B", "C"]
    result = solve_integer_recovery(
        flights, options, columns, cancellation_costs=cancel_costs
    )

    assert result.objective == pytest.approx(
        _exhaustive_reference(flights, options, columns, cancel_costs)
    )
    assert result.objective == pytest.approx(4.0)
    assert set(result.selected_pairing_ids) == {"A-retimed-B", "C-single"}
    assert result.flight_actions["A"]["action"] == "retime"
    assert result.flight_actions["A"]["retime_minutes"] == 30
    assert result.flight_actions["B"]["action"] == "operate"
    assert result.flight_actions["C"]["action"] == "operate"
    assert result.cancelled_flights == ()


def test_integer_recovery_can_explicitly_cancel_flight_without_pairing():
    result = solve_integer_recovery(
        ["A", "B"],
        [FlightOption("A_ORIG", "A")],
        [PairingColumn("A-only", ("A_ORIG",), cost=1.0)],
        cancellation_costs={"A": 10.0, "B": 7.0},
    )
    assert result.objective == pytest.approx(8.0)
    assert result.flight_actions["A"]["action"] == "operate"
    assert result.flight_actions["B"]["action"] == "cancel"
    assert result.cancelled_flights == ("B",)


def test_integer_recovery_rejects_two_options_for_same_flight_in_one_pairing():
    options = [
        FlightOption("A_ORIG", "A"),
        FlightOption("A_PLUS_15", "A", "retime", 15, 1.0),
    ]
    bad_column = PairingColumn("bad", ("A_ORIG", "A_PLUS_15"), cost=1.0)
    with pytest.raises(ValueError, match="multiple options for the same base flight"):
        solve_integer_recovery(
            ["A"], options, [bad_column], cancellation_costs={"A": 10.0}
        )
