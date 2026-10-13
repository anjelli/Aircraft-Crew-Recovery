from __future__ import annotations

from datetime import datetime
from itertools import combinations

from column_generation import PairingColumn
from crew_recovery.aircraft_rotation import Aircraft
from crew_recovery.joint_recovery import solve_joint_operate_cancel, solve_joint_operate_cancel_greedy


def _flight(fid, origin, destination, std, sta, eqp="320", mgt=45):
    return {
        "flight_id": fid,
        "From": origin,
        "To": destination,
        "STD": datetime.fromisoformat(std),
        "STA": datetime.fromisoformat(sta),
        "Eqp": eqp,
        "mgt_min": mgt,
    }


def _brute_force_one_aircraft(flights, pairings, aircraft):
    """Independent exhaustive enumerator for tiny one-aircraft instances."""
    best = len(flights) + 1
    for size in range(len(pairings) + 1):
        for chosen in combinations(pairings, size):
            counts = {f["flight_id"]: 0 for f in flights}
            for pairing in chosen:
                for fid in pairing.flight_ids:
                    counts[fid] += 1
            if any(value > 1 for value in counts.values()):
                continue
            operated = [f for f in flights if counts[f["flight_id"]] == 1]
            cancellations = len(flights) - len(operated)
            if cancellations >= best:
                continue
            # Independent route feasibility check: one chronological path must
            # start at the aircraft's station/time and obey station + MGT.
            route_ok = True
            route = sorted(operated, key=lambda f: (f["STD"], f["flight_id"]))
            if route:
                first = route[0]
                if (
                    any(f["Eqp"] != aircraft.equipment for f in route)
                    or first["From"] != aircraft.start_station
                    or first["STD"] < aircraft.available_at
                ):
                    route_ok = False
                for previous, current in zip(route, route[1:]):
                    gap = (current["STD"] - previous["STA"]).total_seconds() / 60
                    if previous["To"] != current["From"] or gap < previous["mgt_min"]:
                        route_ok = False
            if route_ok:
                best = cancellations
    return best


def test_joint_model_matches_exhaustive_minimum_cancellations():
    flights = [
        _flight("F1", "SIN", "KUL", "2026-10-25T08:00:00", "2026-10-25T09:00:00"),
        _flight("F2", "KUL", "SIN", "2026-10-25T10:00:00", "2026-10-25T11:00:00"),
    ]
    pairings = [
        PairingColumn("P_BOTH", ("F1", "F2"), cost=2.0, pairing_type="two-duty", is_multi_day=True, num_duties=2),
        PairingColumn("P1", ("F1",), cost=1.0, pairing_type="single", num_duties=1),
        PairingColumn("P2", ("F2",), cost=1.0, pairing_type="single", num_duties=1),
    ]
    tail = Aircraft("LINE_1", "320", "SIN", datetime.fromisoformat("2026-10-25T08:00:00"))
    expected = _brute_force_one_aircraft(flights, pairings, tail)
    result = solve_joint_operate_cancel(flights, pairings, [tail])
    assert expected == 0
    assert result.cancellation_count == expected
    assert set(result.aircraft_by_flight) == {"F1", "F2"}
    assert set(result.selected_pairing_ids) in ({"P_BOTH"}, {"P1", "P2"})


def test_joint_model_cancels_flight_without_compatible_aircraft():
    flights = [
        _flight("F320", "SIN", "KUL", "2026-10-25T08:00:00", "2026-10-25T09:00:00", eqp="320"),
        _flight("F32N", "KUL", "SIN", "2026-10-25T10:00:00", "2026-10-25T11:00:00", eqp="32N"),
    ]
    pairings = [
        PairingColumn("P320", ("F320",), cost=1.0, pairing_type="single"),
        PairingColumn("P32N", ("F32N",), cost=1.0, pairing_type="single"),
    ]
    tail = Aircraft("LINE_1", "320", "SIN", datetime.fromisoformat("2026-10-25T08:00:00"))
    expected = _brute_force_one_aircraft(flights, pairings, tail)
    result = solve_joint_operate_cancel(flights, pairings, [tail])
    assert expected == 1
    assert result.cancellation_count == expected
    assert result.cancelled_flights == ("F32N",)
    assert set(result.aircraft_by_flight) == {"F320"}



def test_greedy_fallback_returns_a_feasible_nonoptimal_plan():
    flights = [
        _flight("F1", "SIN", "KUL", "2026-10-25T08:00:00", "2026-10-25T09:00:00"),
        _flight("F2", "KUL", "SIN", "2026-10-25T10:00:00", "2026-10-25T11:00:00"),
        _flight("F3", "SIN", "BKK", "2026-10-25T12:00:00", "2026-10-25T13:00:00"),
    ]
    pairings = [
        PairingColumn("P12", ("F1", "F2"), cost=2.0, pairing_type="validated"),
        PairingColumn("P3", ("F3",), cost=1.0, pairing_type="validated"),
    ]
    tail = Aircraft("LINE_1", "320", "SIN", datetime.fromisoformat("2026-10-25T08:00:00"))
    result = solve_joint_operate_cancel_greedy(flights, pairings, [tail])
    assert result.cancellation_count == 0
    assert set(result.aircraft_by_flight) == {"F1", "F2", "F3"}
    assert set(result.selected_pairing_ids) == {"P12", "P3"}
    assert result.optimal_over_supplied_columns is False
    assert result.minimum_cancellation_count_proven is False


def test_greedy_fallback_does_not_claim_incompatible_flight_is_operated():
    flights = [
        _flight("F1", "SIN", "KUL", "2026-10-25T08:00:00", "2026-10-25T09:00:00"),
        _flight("F2", "KUL", "SIN", "2026-10-25T10:00:00", "2026-10-25T11:00:00", eqp="32N"),
    ]
    pairings = [
        PairingColumn("P1", ("F1",), cost=1.0),
        PairingColumn("P2", ("F2",), cost=1.0),
    ]
    tail = Aircraft("LINE_1", "320", "SIN", datetime.fromisoformat("2026-10-25T08:00:00"))
    result = solve_joint_operate_cancel_greedy(flights, pairings, [tail])
    assert result.cancelled_flights == ("F2",)
    assert set(result.aircraft_by_flight) == {"F1"}
