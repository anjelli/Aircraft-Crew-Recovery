from datetime import datetime

from column_generation import PairingColumn
from crew_recovery.pairing_scoring import pairing_efficiency_score


def test_efficiency_score_prefers_lower_cost_per_covered_sector():
    flights = [
        {"flight_id": "F1", "STD": datetime(2026, 10, 25, 8), "STA": datetime(2026, 10, 25, 9)},
        {"flight_id": "F2", "STD": datetime(2026, 10, 25, 10), "STA": datetime(2026, 10, 25, 11)},
    ]
    one = PairingColumn("ONE", ("F1",), cost=785, num_duties=1)
    two = PairingColumn("TWO", ("F1", "F2"), cost=785, num_duties=1)

    assert pairing_efficiency_score(two, flights) < pairing_efficiency_score(one, flights)
