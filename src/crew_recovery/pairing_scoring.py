"""Pairing-column ranking features for online candidate ordering.

The baseline score is a transparent heuristic, not a trained ML model. The
callback API permits a learned string/pairing scorer later; all generated
columns are retained regardless of score so scoring cannot silently remove an
optimal solution from the declared candidate pool.
"""
from __future__ import annotations

from math import isfinite
from typing import Mapping, Sequence

from column_generation import PairingColumn


def pairing_efficiency_score(
    column: PairingColumn,
    flights: Sequence[Mapping[str, object]],
) -> float:
    """Lower is preferred: salary cost per covered sector, then duty count."""
    flight_lookup = {str(f["flight_id"]): f for f in flights}
    unknown = set(column.flight_ids) - set(flight_lookup)
    if unknown:
        raise ValueError(f"Unknown flights in pairing {column.column_id}: {sorted(unknown)[:10]}")
    value = float(column.cost) / len(column.flight_ids) + 1e-6 * int(column.num_duties)
    if not isfinite(value) or value < 0:
        raise ValueError(f"Invalid score for pairing {column.column_id}")
    return value
