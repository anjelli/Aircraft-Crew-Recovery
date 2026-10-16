from __future__ import annotations

from datetime import datetime

import pytest

from column_generation import PairingColumn
from crew_recovery.aircraft_rotation import Aircraft
from crew_recovery.flow_recovery import solve_joint_operate_cancel_flow
from crew_recovery.joint_recovery import solve_joint_operate_cancel


def _flight(fid, origin, destination, std, sta, eqp="320", mgt=45):
    return {
        "flight_id": fid, "From": origin, "To": destination,
        "STD": datetime.fromisoformat(std), "STA": datetime.fromisoformat(sta),
        "Eqp": eqp, "mgt_min": mgt,
    }


def _tail(tid, station, eqp="320", at="2026-10-25T00:00"):
    return Aircraft(tail_id=tid, equipment=eqp, start_station=station, available_at=datetime.fromisoformat(at))


def _pairing(pid, *fids, cost=1.0):
    return PairingColumn(column_id=pid, flight_ids=tuple(fids), cost=cost)


def test_flow_operates_chain_and_assigns_one_tail():
    flights = [
        _flight("F1", "SIN", "BKK", "2026-10-25T08:00", "2026-10-25T10:00"),
        _flight("F2", "BKK", "SIN", "2026-10-25T11:00", "2026-10-25T13:00"),
    ]
    result = solve_joint_operate_cancel_flow(flights, [_pairing("P1", "F1", "F2")], [_tail("T1", "SIN")])
    assert result.cancelled_flights == ()
    assert result.selected_pairing_ids == ("P1",)
    assert result.aircraft_by_flight == {"F1": "T1", "F2": "T1"}
    assert result.minimum_cancellation_count_proven and result.optimal_over_supplied_columns


def test_flow_pairings_are_all_or_nothing():
    # F2 cannot be flown (no tail at KUL), so the whole pairing {F1, F2} is dropped.
    flights = [
        _flight("F1", "SIN", "BKK", "2026-10-25T08:00", "2026-10-25T10:00"),
        _flight("F2", "KUL", "SIN", "2026-10-25T12:00", "2026-10-25T14:00"),
    ]
    result = solve_joint_operate_cancel_flow(flights, [_pairing("P1", "F1", "F2")], [_tail("T1", "SIN")])
    assert set(result.cancelled_flights) == {"F1", "F2"}
    assert result.aircraft_by_flight == {}


def test_flow_enforces_mgt_and_equipment_and_missing_equipment_family():
    flights = [
        _flight("F1", "SIN", "BKK", "2026-10-25T08:00", "2026-10-25T10:00"),
        _flight("F2", "BKK", "SIN", "2026-10-25T10:30", "2026-10-25T12:30"),  # 30 < 45 min MGT
        _flight("F3", "SIN", "HKG", "2026-10-25T09:00", "2026-10-25T12:00", eqp="32Q"),  # no 32Q tail
    ]
    pairings = [_pairing("P1", "F1"), _pairing("P2", "F2"), _pairing("P3", "F3")]
    result = solve_joint_operate_cancel_flow(flights, pairings, [_tail("T1", "SIN")])
    assert len(result.cancelled_flights) == 2
    assert "F3" in result.cancelled_flights
    assert result.aircraft_by_flight.keys() <= {"F1", "F2"}


def test_flow_flags_flights_without_any_pairing():
    flights = [_flight("F1", "SIN", "BKK", "2026-10-25T08:00", "2026-10-25T10:00")]
    result = solve_joint_operate_cancel_flow(flights, [], [_tail("T1", "SIN")])
    assert result.cancelled_flights == ("F1",)
    assert result.cancellation_reasons == {"F1": "NO_VALID_PAIRING_CANDIDATE"}


def test_flow_breaks_ties_by_pairing_cost():
    flights = [
        _flight("F1", "SIN", "BKK", "2026-10-25T08:00", "2026-10-25T10:00"),
        _flight("F2", "BKK", "SIN", "2026-10-25T11:00", "2026-10-25T13:00"),
    ]
    pairings = [
        _pairing("EXPENSIVE", "F1", "F2", cost=9.0),
        _pairing("CHEAP_1", "F1", cost=1.0),
        _pairing("CHEAP_2", "F2", cost=1.5),
    ]
    result = solve_joint_operate_cancel_flow(flights, pairings, [_tail("T1", "SIN")])
    assert result.cancelled_flights == ()
    assert result.selected_pairing_ids == ("CHEAP_1", "CHEAP_2")
    assert result.secondary_objective == pytest.approx(2.5)


def test_flow_matches_exact_joint_milp_on_two_tail_instance():
    flights = [
        _flight("A1", "SIN", "BKK", "2026-10-25T06:00", "2026-10-25T08:00"),
        _flight("A2", "BKK", "SIN", "2026-10-25T09:00", "2026-10-25T11:00"),
        _flight("A3", "SIN", "KUL", "2026-10-25T12:00", "2026-10-25T13:00"),
        _flight("B1", "KUL", "SIN", "2026-10-25T07:00", "2026-10-25T08:00"),
        _flight("B2", "SIN", "BKK", "2026-10-25T09:00", "2026-10-25T11:00"),
        _flight("B3", "BKK", "KUL", "2026-10-25T12:00", "2026-10-25T14:00"),
        _flight("C1", "HKG", "SIN", "2026-10-25T10:00", "2026-10-25T14:00"),
    ]
    pairings = [
        _pairing("P1", "A1", "A2"), _pairing("P2", "A2", "A3"), _pairing("P3", "A3"),
        _pairing("P4", "B1", "B2"), _pairing("P5", "B2", "B3"), _pairing("P6", "B1"),
        _pairing("P7", "C1"), _pairing("P8", "A1"),
    ]
    tails = [_tail("T1", "SIN"), _tail("T2", "KUL")]
    flow = solve_joint_operate_cancel_flow(flights, pairings, tails)
    milp = solve_joint_operate_cancel(flights, pairings, tails, time_limit_seconds=60)
    assert flow.cancellation_count == milp.cancellation_count
    assert flow.secondary_objective == pytest.approx(milp.secondary_objective)
    assert set(flow.aircraft_by_flight) == set(f["flight_id"] for f in flights) - set(flow.cancelled_flights)