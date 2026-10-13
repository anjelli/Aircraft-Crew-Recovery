"""Safe ranking of feasible aircraft connections; never prunes candidates."""
from __future__ import annotations

from typing import Mapping


def connection_features(first: Mapping[str, object], second: Mapping[str, object]) -> dict[str, float]:
    """Features for ranking a feasible connection, not for legality decisions."""
    gap = (second["STD"] - first["STA"]).total_seconds() / 60.0
    required = first.get("mgt_min")
    if required is None:
        required = first.get("MGT_td")
        required = required.total_seconds() / 60.0 if hasattr(required, "total_seconds") else None
    if required is None:
        raise ValueError(f"Missing MGT for preceding flight {first.get('flight_id')!r}")
    required = float(required)
    if gap < required:
        raise ValueError("connection_features accepts only MGT-feasible connections")
    return {
        "ground_gap_minutes": float(gap),
        "mgt_slack_minutes": float(gap - required),
        "returns_to_previous_origin": float(
            str(second["To"]).strip().upper() == str(first["From"]).strip().upper()
        ),
        "same_equipment": float(str(first.get("Eqp", "")).strip() == str(second.get("Eqp", "")).strip()),
    }


def connection_priority_score(first: Mapping[str, object], second: Mapping[str, object]) -> float:
    """Lower is preferred: immediate return, then shorter feasible ground gap.

    This is a transparent heuristic score, not an ML prediction. It must be used
    only for ordering/tie-breaking. The caller must retain all feasible arcs so
    a potentially optimal connection is never discarded by the score.
    """
    features = connection_features(first, second)
    return (
        features["ground_gap_minutes"]
        + 10_000.0 * (1.0 - features["returns_to_previous_origin"])
        + 1_000.0 * (1.0 - features["same_equipment"])
    )
