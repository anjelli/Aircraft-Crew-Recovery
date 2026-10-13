"""Resource-constrained generation/pricing for one-, two-, and three-duty pairings.

The declared domain is bounded by max_duties, max_sectors_per_duty, permitted
layover stations, the max-layover rule, and the supplied validator. Exactness is
only over that domain and the provided schedule/rule data.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from math import isfinite
from typing import Callable, Mapping, Sequence

from column_generation import PairingColumn
from crew_recovery.independent_validator import fdp_band


Flight = Mapping[str, object]
PairingValidator = Callable[[Sequence[Flight], Sequence[Sequence[int]], str], Sequence[object]]


@dataclass(frozen=True)
class _Duty:
    flights: tuple[Flight, ...]
    ids: tuple[str, ...]
    start_station: str
    end_station: str
    start_time: object
    end_time: object


class ResourceConstrainedPairingPricer:
    """Generate improving legal pairing columns from flight/rule data on demand.

    The validator callback must independently validate the supplied ordered
    flights using the given duty splits and pairing type. No candidate is
    returned unless that callback reports zero violations.
    """

    def __init__(
        self,
        flights: Sequence[Flight],
        *,
        fdp_limits: Mapping[str, Sequence[float]],
        duty_start_buffer: int,
        fdp_end_buffer: int,
        duty_end_buffer: int,
        max_layover_hours: float,
        layover_stations: Sequence[str],
        validator: PairingValidator,
        pairing_cost: float = 1.0,
        hub: str = "SIN",
        max_sectors_per_duty: int = 4,
        max_duties: int = 2,
    ) -> None:
        if not flights:
            raise ValueError("flights must not be empty")
        if pairing_cost < 0 or not isfinite(float(pairing_cost)):
            raise ValueError("pairing_cost must be finite and nonnegative")
        if max_layover_hours <= 0 or not isfinite(float(max_layover_hours)):
            raise ValueError("max_layover_hours must be finite and positive")
        if max_sectors_per_duty < 1:
            raise ValueError("max_sectors_per_duty must be >= 1")
        if max_duties not in (1, 2, 3):
            raise ValueError("max_duties must be 1, 2, or 3")
        records = [dict(f) for f in flights if bool(f.get("active", True))]
        ids = [str(f.get("flight_id", "")) for f in records]
        if any(not fid for fid in ids) or len(ids) != len(set(ids)):
            raise ValueError("active flights must have unique non-empty flight_id values")
        for f in records:
            for key in ("From", "To", "STD", "STA", "STD_local", "STA_local"):
                if f.get(key) is None:
                    raise ValueError(f"flight {f['flight_id']!r} is missing {key}")
        self.flights = tuple(sorted(records, key=lambda f: (f["STD"], str(f["flight_id"]))))
        self.flight_ids = frozenset(ids)
        self.fdp_limits = {str(k): tuple(float(x) for x in v) for k, v in fdp_limits.items()}
        self.dsb = int(duty_start_buffer)
        self.feb = int(fdp_end_buffer)
        self.deb = int(duty_end_buffer)
        self.max_layover = timedelta(hours=float(max_layover_hours))
        self.layover_stations = frozenset(str(s).strip().upper() for s in layover_stations)
        self.validator = validator
        self.pairing_cost = float(pairing_cost)
        self.hub = str(hub).strip().upper()
        self.max_sectors = int(max_sectors_per_duty)
        self.max_duties = int(max_duties)
        self._duties: tuple[_Duty, ...] | None = None
        self._states_explored = 0

    @staticmethod
    def _airport(flight: Flight, key: str) -> str:
        return str(flight[key]).strip().upper()

    @staticmethod
    def _minutes(flight: Flight) -> float | None:
        if flight.get("mgt_min") is not None:
            try:
                value = float(flight["mgt_min"])
                return value if isfinite(value) else None
            except (TypeError, ValueError):
                return None
        value = flight.get("MGT_td")
        if hasattr(value, "total_seconds"):
            return float(value.total_seconds()) / 60.0
        return None

    def _duty_hours(self, seq: Sequence[Flight]) -> float:
        end_buffer = self.deb if len(seq) >= self.max_sectors else self.feb
        start = seq[0]["STD"] - timedelta(minutes=self.dsb)
        end = seq[-1]["STA"] + timedelta(minutes=end_buffer)
        return (end - start).total_seconds() / 3600.0

    def _duty_feasible(self, seq: Sequence[Flight]) -> bool:
        if not seq or len(seq) > self.max_sectors:
            return False
        band = fdp_band(seq[0]["STD_local"] - timedelta(minutes=self.dsb))
        limits = self.fdp_limits.get(band)
        if limits is None or len(limits) < len(seq):
            return False
        return self._duty_hours(seq) <= limits[len(seq) - 1] + 1e-9

    def _can_connect(self, seq: Sequence[Flight], nxt: Flight) -> bool:
        prev = seq[-1]
        if self._airport(prev, "To") != self._airport(nxt, "From"):
            return False
        mgt = self._minutes(prev)
        if mgt is None:
            return False
        if len(seq) == 2 and str(prev.get("Eqp", "")) != str(nxt.get("Eqp", "")):
            mgt = 90.0
        return nxt["STD"] >= prev["STA"] + timedelta(minutes=mgt)

    def _enumerate_duties(self) -> tuple[_Duty, ...]:
        if self._duties is not None:
            return self._duties
        by_origin: dict[str, list[Flight]] = {}
        for f in self.flights:
            by_origin.setdefault(self._airport(f, "From"), []).append(f)
        for group in by_origin.values():
            group.sort(key=lambda f: (f["STD"], str(f["flight_id"])))

        duties: dict[tuple[str, ...], _Duty] = {}

        def visit(path: tuple[Flight, ...]) -> None:
            self._states_explored += 1
            if not self._duty_feasible(path):
                return
            ids = tuple(str(f["flight_id"]) for f in path)
            duty = _Duty(
                flights=path,
                ids=ids,
                start_station=self._airport(path[0], "From"),
                end_station=self._airport(path[-1], "To"),
                start_time=path[0]["STD"],
                end_time=path[-1]["STA"],
            )
            duties[ids] = duty
            if len(path) >= self.max_sectors:
                return
            visited = set(ids)
            for nxt in by_origin.get(duty.end_station, ()):
                if str(nxt["flight_id"]) in visited:
                    continue
                if nxt["STD"] < path[-1]["STD"]:
                    continue
                if not self._can_connect(path, nxt):
                    continue
                extended = path + (nxt,)
                if self._duty_feasible(extended):
                    visit(extended)

        allowed_starts = {self.hub} | set(self.layover_stations)
        for flight in self.flights:
            if self._airport(flight, "From") in allowed_starts:
                visit((flight,))
        self._duties = tuple(duties.values())
        return self._duties

    @staticmethod
    def _column_id(pairing_type: str, ids: Sequence[str]) -> str:
        digest = sha256((pairing_type + "|" + "|".join(ids)).encode("utf-8")).hexdigest()[:16]
        return f"rcp-{digest}"

    def _column(self, seq: Sequence[Flight], splits: Sequence[Sequence[int]], pairing_type: str) -> PairingColumn | None:
        ids = tuple(str(f["flight_id"]) for f in seq)
        if len(set(ids)) != len(ids) or not set(ids).issubset(self.flight_ids):
            return None
        try:
            violations = self.validator(seq, splits, pairing_type)
        except (TypeError, ValueError, KeyError, IndexError):
            return None
        if violations:
            return None
        return PairingColumn(
            column_id=self._column_id(pairing_type, ids),
            flight_ids=ids,
            cost=self.pairing_cost,
            pairing_type=pairing_type,
            is_multi_day=len(splits) > 1,
            num_duties=len(splits),
        )

    def _candidate_columns(self):
        duties = self._enumerate_duties()
        # Single-duty hub turnarounds.
        for duty in duties:
            if duty.start_station == self.hub and duty.end_station == self.hub and len(duty.ids) >= 2:
                splits = [list(range(len(duty.ids)))]
                col = self._column(duty.flights, splits, "single_day")
                if col is not None:
                    yield col
        # Duty-chain index for pairings that start at the hub and return to it.
        inbound: dict[str, list[_Duty]] = {}
        outbound: dict[str, list[_Duty]] = {}
        for duty in duties:
            if duty.start_station == self.hub and duty.end_station in self.layover_stations:
                outbound.setdefault(duty.end_station, []).append(duty)
            if duty.start_station in self.layover_stations and duty.end_station == self.hub:
                inbound.setdefault(duty.start_station, []).append(duty)

        # Two-duty multi-day pairings: hub -> permitted layover -> hub.
        if self.max_duties >= 2:
            for station in sorted(set(outbound) & set(inbound)):
                for first in outbound[station]:
                    for second in inbound[station]:
                        if set(first.ids) & set(second.ids):
                            continue
                        if second.start_time <= first.end_time:
                            continue
                        if second.start_time - first.end_time > self.max_layover:
                            continue
                        seq = first.flights + second.flights
                        split_at = len(first.flights)
                        splits = [list(range(split_at)), list(range(split_at, len(seq)))]
                        col = self._column(seq, splits, "multi_day")
                        if col is not None:
                            yield col

        # Three-duty pairings: hub -> layover A -> layover B -> hub.
        if self.max_duties >= 3:
            middle_by_start: dict[str, list[_Duty]] = {}
            for duty in duties:
                if duty.start_station in self.layover_stations and duty.end_station in self.layover_stations:
                    middle_by_start.setdefault(duty.start_station, []).append(duty)
            for first_station in sorted(outbound):
                for first in outbound[first_station]:
                    for middle in middle_by_start.get(first.end_station, ()):
                        if middle.start_time <= first.end_time:
                            continue
                        if middle.start_time - first.end_time > self.max_layover:
                            continue
                        if set(first.ids) & set(middle.ids):
                            continue
                        for third in inbound.get(middle.end_station, ()):
                            if third.start_time <= middle.end_time:
                                continue
                            if third.start_time - middle.end_time > self.max_layover:
                                continue
                            if set(first.ids) & set(third.ids) or set(middle.ids) & set(third.ids):
                                continue
                            seq = first.flights + middle.flights + third.flights
                            split1 = len(first.flights)
                            split2 = split1 + len(middle.flights)
                            splits = [
                                list(range(0, split1)),
                                list(range(split1, split2)),
                                list(range(split2, len(seq))),
                            ]
                            col = self._column(seq, splits, "three_duty")
                            if col is not None:
                                yield col

    @property
    def states_explored(self) -> int:
        """Number of partial duty paths visited by the current enumerator."""
        return self._states_explored

    def generate_candidates(self) -> tuple[PairingColumn, ...]:
        """Enumerate all independently validated columns in the declared pairing domain.

        This is useful when a coupled objective (for example, aircraft recovery)
        makes crew-only reduced-cost filtering insufficient. The method does not
        claim that the declared domain contains every operationally legal pairing.
        """
        unique: dict[str, PairingColumn] = {}
        for column in self._candidate_columns():
            unique.setdefault(column.column_id, column)
        return tuple(unique[key] for key in sorted(unique))

    def __call__(self, dual_prices, active_column_ids, reduced_cost_tolerance, batch_size):
        # Local import avoids a module-level cycle with integrated_column_generation.
        from crew_recovery.integrated_column_generation import PricingBatch
        from column_generation import reduced_cost

        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        active = set(active_column_ids)
        scored: dict[str, tuple[PairingColumn, float]] = {}
        states_before = self._states_explored
        candidate_count = 0
        best_rc: float | None = None
        for column in self._candidate_columns():
            candidate_count += 1
            if column.column_id in active:
                continue
            rc = reduced_cost(column, dual_prices)
            best_rc = rc if best_rc is None else min(best_rc, rc)
            if rc < -float(reduced_cost_tolerance):
                scored[column.column_id] = (column, rc)
        ordered = sorted(scored.values(), key=lambda x: (x[1], x[0].column_id))
        selected = tuple(c for c, _ in ordered[:batch_size])
        explored_this_call = (self._states_explored - states_before) + candidate_count
        return PricingBatch(
            columns=selected,
            best_reduced_cost=best_rc,
            certified_no_improving_column=not ordered,
            explored_states=explored_this_call,
            status="exact-duty-enumeration-two-duty-pairing-domain",
        )
