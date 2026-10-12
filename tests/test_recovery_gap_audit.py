from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from audit_recovery_gap import audit_uncovered, audit_sequence_universes


def test_candidate_covered_but_uncovered_flights_are_not_misclassified():
    # These are the two flight instances observed in the 1W pricing audit.
    coverage = pd.DataFrame([
        {
            "flight_id": "425_20261027T172500",
            "lp_uncovered_slack": 1.0,
            "candidate_columns_covering_flight": 2,
        },
        {
            "flight_id": "457_20261026T071500",
            "lp_uncovered_slack": 1.0,
            "candidate_columns_covering_flight": 1,
        },
        {
            "flight_id": "NO_CANDIDATE",
            "lp_uncovered_slack": 1.0,
            "candidate_columns_covering_flight": 0,
        },
    ])
    columns = pd.DataFrame([
        {"column_id": "A", "pairing_type": "single_day", "flight_ids": "425_20261027T172500|X"},
        {"column_id": "B", "pairing_type": "single_day", "flight_ids": "425_20261027T172500|Y"},
        {"column_id": "C", "pairing_type": "multi_day", "flight_ids": "457_20261026T071500|Z"},
    ])

    result = audit_uncovered(coverage, columns).set_index("flight_id")
    assert result.loc["425_20261027T172500", "triage_class"] == "CANDIDATE_EXISTS_NOT_SELECTED"
    assert result.loc["425_20261027T172500", "candidate_column_ids"] == "A|B"
    assert result.loc["457_20261026T071500", "triage_class"] == "CANDIDATE_EXISTS_NOT_SELECTED"
    assert result.loc["457_20261026T071500", "candidate_column_ids"] == "C"
    assert result.loc["NO_CANDIDATE", "triage_class"] == "NO_PRICING_CANDIDATE"


def test_builder_pricer_sequence_audit_compares_exact_ordered_ids():
    builder = pd.DataFrame([
        {"source_pairing_id": "B1", "pairing_type": "single_day", "sequence": "F1|F2", "independent_check": "PASS"},
        {"source_pairing_id": "B2", "pairing_type": "single_day", "sequence": "F3|F4", "independent_check": "PASS"},
    ])
    columns = pd.DataFrame([
        {"column_id": "P1", "pairing_type": "single_day", "flight_ids": "F1|F2"},
        {"column_id": "P2", "pairing_type": "multi_day", "flight_ids": "F2|F1"},
        {"column_id": "P3", "pairing_type": "multi_day", "flight_ids": "F5|F6"},
    ])

    result = audit_sequence_universes(builder, columns).set_index("sequence")
    assert result.loc["F1|F2", "universe_difference"] == "BOTH"
    assert result.loc["F3|F4", "universe_difference"] == "BUILDER_ONLY"
    assert result.loc["F2|F1", "universe_difference"] == "PRICER_ONLY"
    assert result.loc["F5|F6", "universe_difference"] == "PRICER_ONLY"
