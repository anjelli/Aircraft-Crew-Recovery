from datetime import datetime

from crew_recovery.connection_scoring import connection_features, connection_priority_score


def _flight(fid, origin, destination, std, sta, mgt=45):
    return {
        "flight_id": fid,
        "From": origin,
        "To": destination,
        "STD": datetime.fromisoformat(std),
        "STA": datetime.fromisoformat(sta),
        "Eqp": "320",
        "mgt_min": mgt,
    }


def test_immediate_return_connection_ranks_ahead_of_later_nonreturn_without_pruning():
    outbound = _flight("OUT", "SIN", "KUL", "2026-10-25T08:00:00", "2026-10-25T09:00:00")
    immediate_return = _flight("BACK1", "KUL", "SIN", "2026-10-25T10:00:00", "2026-10-25T11:00:00")
    later_nonreturn = _flight("OTHER", "KUL", "BKK", "2026-10-25T12:00:00", "2026-10-25T13:00:00")

    assert connection_features(outbound, immediate_return)["returns_to_previous_origin"] == 1.0
    assert connection_priority_score(outbound, immediate_return) < connection_priority_score(outbound, later_nonreturn)
    # The score is a ranking only: both candidates remain eligible for the caller.
    assert outbound["To"] == immediate_return["From"] == later_nonreturn["From"]
