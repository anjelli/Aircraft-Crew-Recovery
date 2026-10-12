"""Adapters from the canonical pairing builder to the coverage-LP columns.

The builder remains responsible for constructing candidate pairings. This module
normalizes those candidates, independently validates them, and preserves
multi-day metadata without duplicating pairing-generation rules.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Callable, Mapping, Sequence

from column_generation import PairingColumn


Pairing = Sequence[Mapping[str, object]]
PairingValidator = Callable[[Pairing, Mapping[str, object]], Sequence[object]]


@dataclass(frozen=True)
class RejectedPairing:
    candidate_id: str
    reason: str


@dataclass(frozen=True)
class PairingColumnBuildResult:
    columns: tuple[PairingColumn, ...]
    rejected: tuple[RejectedPairing, ...]
    duplicate_sequences: int


def infer_duty_splits(
    pairing: Pairing,
    metadata: Mapping[str, object],
) -> list[list[int]]:
    """Infer duty boundaries only where the builder's metadata supports it.

    The canonical builder emits simple two-sector multi-day pairings and
    combined pairings with a named layover station. Ambiguous boundaries fail
    closed rather than silently validating a guessed duty decomposition.
    """
    n = len(pairing)
    if n == 0:
        raise ValueError("pairing contains no flights")

    explicit = metadata.get("duty_splits")
    if explicit is not None:
        splits = [[int(i) for i in group] for group in explicit]  # type: ignore[union-attr]
        flattened = [i for group in splits for i in group]
        if flattened != list(range(n)):
            raise ValueError("metadata duty_splits must partition the ordered flights")
        return splits

    pairing_type = str(metadata.get("pairing_type", "unknown"))
    if pairing_type == "single_day":
        return [list(range(n))]
    if pairing_type == "multi_day" and n == 2:
        return [[0], [1]]

    if pairing_type in {"combined_post_layover", "combined_pre_layover"}:
        station = str(metadata.get("layover_station", "")).strip()
        if not station:
            raise ValueError(f"{pairing_type} metadata needs layover_station")
        candidates = [
            i for i in range(1, n)
            if str(pairing[i - 1].get("To", "")) == station
            and str(pairing[i].get("From", "")) == station
        ]
        if not candidates:
            raise ValueError(f"No duty boundary found at layover station {station!r}")
        # If the station is visited more than once, the longest same-station
        # ground gap is the best-supported layover boundary. Ambiguity is
        # rejected if two candidates have indistinguishable gaps.
        def gap_minutes(i: int) -> float:
            before, after = pairing[i - 1], pairing[i]
            sta, std = before.get("STA"), after.get("STD")
            if sta is None or std is None:
                raise ValueError("pairing records need STA and STD to infer duty splits")
            return (std - sta).total_seconds() / 60.0  # type: ignore[operator]
        ranked = sorted(((gap_minutes(i), i) for i in candidates), reverse=True)
        if len(ranked) > 1 and abs(ranked[0][0] - ranked[1][0]) < 1e-9:
            raise ValueError("Ambiguous layover boundary; provide explicit duty_splits")
        boundary = ranked[0][1]
        return [list(range(boundary)), list(range(boundary, n))]

    raise ValueError(
        f"Cannot infer duty boundaries for pairing_type={pairing_type!r}; "
        "provide explicit duty_splits in metadata"
    )


def independent_pairing_validator(rules, *, validator_module=None) -> PairingValidator:
    """Create a validator callback backed by independent_validator.check_pairing."""
    if validator_module is None:
        import independent_validator as validator_module  # type: ignore[no-redef]

    def validate(pairing: Pairing, metadata: Mapping[str, object]) -> Sequence[object]:
        splits = infer_duty_splits(pairing, metadata)
        return validator_module.check_pairing(
            "generated-candidate",
            list(pairing),
            splits,
            rules,
        )

    return validate


def _candidate_id(pairing: Pairing, pairing_type: str) -> str:
    ordered_ids = [str(f.get("flight_id", "")) for f in pairing]
    if any(not fid for fid in ordered_ids):
        raise ValueError("every builder flight record must have a non-empty flight_id")
    digest = sha256((pairing_type + "|" + "|".join(ordered_ids)).encode("utf-8")).hexdigest()[:16]
    return f"pairing-{digest}"


def build_pairing_columns(
    pairings: Sequence[Pairing],
    metadata: Sequence[Mapping[str, object]],
    active_flight_ids: Sequence[str],
    *,
    validator: PairingValidator,
    cost: float | Callable[[Pairing, Mapping[str, object]], float] = 1.0,
) -> PairingColumnBuildResult:
    """Convert builder output into validated coverage columns.

    Pairings are rejected if they contain inactive/unknown flights, fail
    independent validation, or cannot be represented unambiguously. Exact
    duplicate sequence/type combinations are deduplicated deterministically.
    """
    if len(pairings) != len(metadata):
        raise ValueError("pairings and metadata must have the same length")
    active = set(map(str, active_flight_ids))
    if not active:
        raise ValueError("active_flight_ids must not be empty")

    accepted: list[PairingColumn] = []
    rejected: list[RejectedPairing] = []
    seen_ids: set[str] = set()
    duplicate_sequences = 0

    for pairing, meta in zip(pairings, metadata):
        pairing_type = str(meta.get("pairing_type", "unknown"))
        candidate_id = _candidate_id(pairing, pairing_type)
        ids = tuple(str(f["flight_id"]) for f in pairing)
        unknown = sorted(set(ids) - active)
        if unknown:
            rejected.append(RejectedPairing(candidate_id, f"inactive_or_unknown_flights:{','.join(unknown)}"))
            continue
        if candidate_id in seen_ids:
            duplicate_sequences += 1
            continue
        try:
            violations = validator(pairing, meta)
        except (TypeError, ValueError, KeyError) as exc:
            rejected.append(RejectedPairing(candidate_id, f"validation_error:{exc}"))
            continue
        if violations:
            labels = []
            for violation in violations:
                if hasattr(violation, "vtype"):
                    labels.append(str(violation.vtype))
                elif isinstance(violation, Mapping):
                    labels.append(str(violation.get("type", violation.get("Violation Type", "violation"))))
                else:
                    labels.append(str(violation))
            rejected.append(RejectedPairing(candidate_id, "independent_validation_failed:" + ",".join(labels)))
            continue

        column_cost = cost(pairing, meta) if callable(cost) else cost
        accepted.append(PairingColumn(
            column_id=candidate_id,
            flight_ids=ids,
            cost=float(column_cost),
            pairing_type=pairing_type,
            is_multi_day=bool(meta.get("is_multi_day", False)),
            num_duties=int(meta.get("num_duties", 1)),
        ))
        seen_ids.add(candidate_id)

    return PairingColumnBuildResult(
        columns=tuple(accepted),
        rejected=tuple(rejected),
        duplicate_sequences=duplicate_sequences,
    )
