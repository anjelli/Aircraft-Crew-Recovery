"""
Crew pairing generation.

Builds candidate crew pairings (single-day turnarounds, multi-day layover
pairings, and pairings that splice a turnaround/layover duty onto an
adjacent layover duty) from a flight schedule, subject to duty/FDP limits
and minimum rest rules.

Public entry points: build_all_pairings, get_time_band_key.
"""

import math
import time
from bisect import bisect_left
from datetime import timedelta
from functools import partial
from itertools import chain
from multiprocessing import Pool, cpu_count

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

TD_10H = timedelta(hours=10)
TD_12H = timedelta(hours=12)
TD_24H = timedelta(hours=24)
EQP_CHANGE_MGT = timedelta(minutes=90)  # min-ground-time when swapping equipment type

HUB = "SIN"
MAX_SECTORS_PER_DUTY = 4  # duties are capped at 4 sectors


# --------------------------------------------------------------------------
# Small, generic helpers
# --------------------------------------------------------------------------

def get_time_band_key(dt):
    hour = dt.hour
    if 6 <= hour < 8:
        return "0600-0759"
    elif 8 <= hour < 15:
        return "0800-1459"
    elif 15 <= hour < 22:
        return "1500-2159"
    else:
        return "2200-0559"


def fast_get_band(duty_start, get_time_band_key, band_cache):
    """Cache band lookups by hour for reuse."""
    h = duty_start.hour
    if h not in band_cache:
        band_cache[h] = get_time_band_key(duty_start)
    return band_cache[h]


def in_date_range(flight, start_date, end_date, date_field="STD_local"):
    """
    True if flight's departure date is in [start_date, end_date].
    start_date/end_date should be datetime.date objects.
    """
    return start_date <= flight[date_field].date() <= end_date


def build_fast_from_groups(df):
    """
    Build prefiltered adjacency: for each 'From' airport, store
    only flights that can realistically follow a flight arriving there.
    """
    grouped = df.groupby("From")
    from_groups_fast = {}
    for airport, group in grouped:
        group_sorted = group.sort_values("STD")
        from_groups_fast[airport] = group_sorted.to_dict("records")
    return from_groups_fast


def build_min_return_hours(from_groups):
    """For each airport, the cheapest block+MGT time (in hours) of a direct flight
    back to the hub. Used to prune branches that can no longer make it home."""
    min_return_hours = {}
    for airport, flights in from_groups.items():
        best = float("inf")
        for f in flights:
            if f["To"] == HUB:
                block_hrs = (f["STA"] - f["STD"]).total_seconds() / 3600.0
                mgt_hrs = f["MGT_td"].total_seconds() / 3600.0
                best = min(best, block_hrs + mgt_hrs)
        if best < float("inf"):
            min_return_hours[airport] = best
    return min_return_hours


def eqp_allowed_single_day(sectors, start_eqp, cur_eqp, next_eqp):
    """Equipment-continuity rule for single-day (hub turnaround) pairings."""
    if sectors == 1:
        return next_eqp == start_eqp   # sector 1->2 must match starting aircraft type
    if sectors == 2:
        return True                     # sector 2->3 (post-turnaround) can change type
    if sectors == 3:
        return next_eqp == cur_eqp      # sector 3->4 must match current aircraft type
    return False


# --------------------------------------------------------------------------
# Shared duty-limit arithmetic
#
# find_single_day_pairings and find_duties both extend a duty flight-by-flight
# and, at every step, need to know (a) whether the duty so far is still within
# its FDP limit and (b) how far a connection can still be pushed before the
# *next* sector would breach its limit. These helpers hold that arithmetic in
# one place instead of duplicating it in both traversal functions.
# --------------------------------------------------------------------------

def _end_buffer(sectors, fdp_end_buffer, duty_end_buffer):
    return duty_end_buffer if sectors >= MAX_SECTORS_PER_DUTY else fdp_end_buffer


def _duty_state(sectors, duty_start, duty_start_local, arrival_time,
                 fdp_end_buffer, duty_end_buffer, get_time_band_key, band_cache, duty_limits):
    """
    Total duty hours so far (duty_start -> arrival_time + end buffer), the
    applicable time band, and that band's FDP limit for this sector count.
    Returns None if the band has no configured limits.
    """
    end_buffer = _end_buffer(sectors, fdp_end_buffer, duty_end_buffer)
    duty_end = arrival_time + timedelta(minutes=end_buffer)
    total_duty = (duty_end - duty_start).total_seconds() / 3600.0

    band = fast_get_band(duty_start_local, get_time_band_key, band_cache)
    limits = duty_limits.get(band)
    if limits is None:
        return None

    fdp_limit = limits[min(sectors - 1, 7)]
    return total_duty, band, fdp_limit


def _exceeds_next_sector_limit(next_sectors, duty_start, next_arrival,
                                 fdp_end_buffer, duty_end_buffer, next_sector_limit):
    """Would adding one more sector (ending at next_arrival) breach the FDP
    limit for that new sector count?"""
    end_buffer = _end_buffer(next_sectors, fdp_end_buffer, duty_end_buffer)
    duty_end = next_arrival + timedelta(minutes=end_buffer)
    total_duty = (duty_end - duty_start).total_seconds() / 3600.0
    return total_duty > next_sector_limit


def _candidate_window(from_groups, from_group_stds, station, cur_sta, mgt_td, sectors):
    """Onward flights from `station`, sorted by STD, together with the index
    of the earliest one that can legally connect from cur_sta/mgt_td."""
    candidates = from_groups.get(station, [])
    candidate_stds = from_group_stds.get(station, [])
    if not candidates:
        return candidates, candidate_stds, 0

    base_required_mgt = mgt_td if sectors != 2 else min(mgt_td, EQP_CHANGE_MGT)
    min_next_std = cur_sta + base_required_mgt
    start_idx = bisect_left(candidate_stds, min_next_std)
    return candidates, candidate_stds, start_idx


def _required_mgt(sectors, mgt_td, cur_eqp, next_eqp):
    """Minimum ground time before the next sector. Only the connection right
    after the hub turnaround (sector 2) is sensitive to an equipment swap."""
    if sectors == 2:
        return mgt_td if next_eqp == cur_eqp else EQP_CHANGE_MGT
    return mgt_td


# --------------------------------------------------------------------------
# Duty/pairing search
# --------------------------------------------------------------------------

def find_single_day_pairings(
    from_groups,
    from_group_stds,
    current_flight,
    duty_limits,
    get_time_band_key,
    band_cache,
    duty_start_buffer,
    fdp_end_buffer,
    duty_end_buffer,
    min_return_hours=None,
    visited_ids=None,
    sectors=1,
    first_std=None,
    first_std_local=None,
    start_eqp=None,
    max_duty_regular=None,
):
    """
    Depth-first search for hub-turnaround pairings: a duty that starts at the
    hub, makes 2-4 sectors, and ends back at the hub, all within one FDP
    window. Equipment continuity is enforced throughout (eqp_allowed_single_day).
    """
    if visited_ids is None:
        visited_ids = {current_flight["flight_id"]}
        first_std = current_flight["STD"]
        first_std_local = current_flight["STD_local"]
        start_eqp = current_flight["Eqp"]

    duty_start = first_std - timedelta(minutes=duty_start_buffer)
    duty_start_local = first_std_local - timedelta(minutes=duty_start_buffer)

    state = _duty_state(
        sectors, duty_start, duty_start_local, current_flight["STA"],
        fdp_end_buffer, duty_end_buffer, get_time_band_key, band_cache, duty_limits,
    )
    if state is None:
        return
    total_duty, band, fdp_limit = state
    if total_duty > fdp_limit:
        return

    # Prune: if we're not home yet, is there still enough duty budget left
    # for even the cheapest direct flight back?
    cur_to = current_flight["To"]
    if cur_to != HUB:
        min_ret = min_return_hours.get(cur_to)
        if min_ret is None:
            return
        if total_duty + min_ret > max_duty_regular:
            return

    if cur_to == HUB and sectors >= 2 and total_duty <= fdp_limit:
        yield [current_flight]

    if sectors == MAX_SECTORS_PER_DUTY:
        return

    cur_sta = current_flight["STA"]
    cur_eqp = current_flight["Eqp"]
    mgt_td = current_flight["MGT_td"]
    next_sector_limit = duty_limits[band][min(sectors, 7)]
    latest_allowed_sta = (
        duty_start + timedelta(hours=next_sector_limit) - timedelta(minutes=fdp_end_buffer)
    )

    candidates, candidate_stds, start_idx = _candidate_window(
        from_groups, from_group_stds, cur_to, cur_sta, mgt_td, sectors
    )
    if not candidates:
        return

    for nxt in candidates[start_idx:]:
        fid = nxt["flight_id"]
        if fid in visited_ids:
            continue

        if nxt["STA"] > latest_allowed_sta:
            continue

        nxt_eqp = nxt["Eqp"]
        if not eqp_allowed_single_day(sectors, start_eqp, cur_eqp, nxt_eqp):
            continue

        required_mgt = _required_mgt(sectors, mgt_td, cur_eqp, nxt_eqp)
        if nxt["STD"] < cur_sta + required_mgt:
            continue

        if _exceeds_next_sector_limit(
            sectors + 1, duty_start, nxt["STA"], fdp_end_buffer, duty_end_buffer, next_sector_limit
        ):
            continue

        visited_ids.add(fid)
        for suffix in find_single_day_pairings(
            from_groups, from_group_stds, nxt, duty_limits, get_time_band_key, band_cache,
            duty_start_buffer, fdp_end_buffer, duty_end_buffer, min_return_hours,
            visited_ids, sectors + 1, first_std, first_std_local, start_eqp, max_duty_regular,
        ):
            yield [current_flight] + suffix
        visited_ids.remove(fid)


def find_duties(
    from_groups,
    from_group_stds,
    current_flight,
    duty_limits,
    get_time_band_key,
    band_cache,
    duty_start_buffer,
    fdp_end_buffer,
    duty_end_buffer,
    min_return_hours=None,
    visited_ids=None,
    sectors=1,
    first_std=None,
    first_std_local=None,
    start_eqp=None,
    max_duty_regular=None,
    start_station=None,
):
    """
    Depth-first search for generic duties (1-4 sectors) starting from any
    allowed duty-start station, ending anywhere. These are the building
    blocks (turnarounds, layover-outbound, layover-inbound) later spliced
    together into multi-day pairings. Unlike find_single_day_pairings, this
    does not enforce equipment continuity and does not require returning to
    the hub.
    """
    if visited_ids is None:
        visited_ids = {current_flight["flight_id"]}
        first_std = current_flight["STD"]
        start_eqp = current_flight["Eqp"]
        start_station = current_flight["From"]
        first_std_local = current_flight["STD_local"]

    duty_start = first_std - timedelta(minutes=duty_start_buffer)
    duty_start_local = first_std_local - timedelta(minutes=duty_start_buffer)

    state = _duty_state(
        sectors, duty_start, duty_start_local, current_flight["STA"],
        fdp_end_buffer, duty_end_buffer, get_time_band_key, band_cache, duty_limits,
    )
    if state is None:
        return
    total_duty, band, fdp_limit = state
    if total_duty > fdp_limit:
        return

    if total_duty <= fdp_limit:
        yield [current_flight]
    if start_station == HUB and current_flight["To"] == HUB:
        return

    if sectors == MAX_SECTORS_PER_DUTY:
        return

    cur_to = current_flight["To"]
    cur_sta = current_flight["STA"]
    cur_eqp = current_flight["Eqp"]
    mgt_td = current_flight["MGT_td"]
    next_sector_limit = duty_limits[band][min(sectors, 7)]
    latest_allowed_sta = (
        duty_start + timedelta(hours=next_sector_limit) - timedelta(minutes=fdp_end_buffer)
    )

    candidates, candidate_stds, start_idx = _candidate_window(
        from_groups, from_group_stds, cur_to, cur_sta, mgt_td, sectors
    )
    if not candidates:
        return

    for nxt in candidates[start_idx:]:
        fid = nxt["flight_id"]
        if fid in visited_ids:
            continue

        if nxt["STA"] > latest_allowed_sta:
            break  # candidates are STD-sorted, so nothing further can qualify

        nxt_eqp = nxt["Eqp"]
        required_mgt = _required_mgt(sectors, mgt_td, cur_eqp, nxt_eqp)
        if nxt["STD"] < cur_sta + required_mgt:
            continue

        if _exceeds_next_sector_limit(
            sectors + 1, duty_start, nxt["STA"], fdp_end_buffer, duty_end_buffer, next_sector_limit
        ):
            continue

        visited_ids.add(fid)
        for suffix in find_duties(
            from_groups, from_group_stds, nxt, duty_limits, get_time_band_key, band_cache,
            duty_start_buffer, fdp_end_buffer, duty_end_buffer, min_return_hours,
            visited_ids, sectors + 1, first_std, first_std_local, start_eqp, max_duty_regular,
            start_station=start_station,
        ):
            yield [current_flight] + suffix
        visited_ids.remove(fid)


# --------------------------------------------------------------------------
# Per-process worker wrappers (used with multiprocessing.Pool)
# --------------------------------------------------------------------------

def process_single_start_flight(
    first_flight, from_groups, from_group_stds, duty_limits, get_time_band_key,
    duty_start_buffer, fdp_end_buffer, duty_end_buffer, min_return_hours, max_duty_regular,
):
    """Process one starting flight independently."""
    band_cache = {}  # each process needs its own cache
    return list(find_single_day_pairings(
        from_groups=from_groups,
        from_group_stds=from_group_stds,
        current_flight=first_flight,
        duty_limits=duty_limits,
        get_time_band_key=get_time_band_key,
        band_cache=band_cache,
        duty_start_buffer=duty_start_buffer,
        fdp_end_buffer=fdp_end_buffer,
        duty_end_buffer=duty_end_buffer,
        min_return_hours=min_return_hours,
        max_duty_regular=max_duty_regular,
    ))


def process_duty_start_flight(
    first_flight, from_groups, from_group_stds, duty_limits, get_time_band_key,
    duty_start_buffer, fdp_end_buffer, duty_end_buffer, min_return_hours, max_duty_regular,
):
    band_cache = {}
    return list(find_duties(
        from_groups=from_groups,
        from_group_stds=from_group_stds,
        current_flight=first_flight,
        duty_limits=duty_limits,
        get_time_band_key=get_time_band_key,
        band_cache=band_cache,
        duty_start_buffer=duty_start_buffer,
        fdp_end_buffer=fdp_end_buffer,
        duty_end_buffer=duty_end_buffer,
        min_return_hours=min_return_hours,
        max_duty_regular=max_duty_regular,
    ))


# --------------------------------------------------------------------------
# Two-day (single layover) pairings
# --------------------------------------------------------------------------

def _required_rest(prev_duty_hours, actual_rest, has_local_night):
    """True if actual_rest satisfies the rest requirement implied by
    prev_duty_hours (and whether a local night was included)."""
    if prev_duty_hours > 16:
        return actual_rest >= TD_24H and has_local_night
    elif prev_duty_hours > 10:
        return actual_rest >= timedelta(hours=math.ceil(prev_duty_hours))
    else:
        return actual_rest >= (TD_10H if has_local_night else TD_12H)


def find_two_day_pairings_optimized(
    from_groups,
    from_group_stds,
    uncovered_ids,
    duty_limits,
    get_time_band_key,
    band_cache,
    duty_start_buffer,
    fdp_end_buffer,
    max_layover_hours, layover_stations, start_date, end_date,
):
    """
    Simple two-sector pairings: a single-sector duty from the hub to a
    layover station, a legal rest, and a single-sector duty straight back.
    """
    max_layover_td = timedelta(hours=max_layover_hours)
    pairings = []
    metadata = []

    sin_departures = [f for f in from_groups.get(HUB, []) if in_date_range(f, start_date, end_date)]

    for f in sin_departures:
        outstation = f["To"]
        if outstation not in layover_stations:
            continue

        duty_start_1 = f["STD"] - timedelta(minutes=duty_start_buffer)
        duty_start_1_local = f["STD_local"] - timedelta(minutes=duty_start_buffer)
        duty_end_1 = f["STA"] + timedelta(minutes=fdp_end_buffer)
        prev_duty_hours = (duty_end_1 - duty_start_1).total_seconds() / 3600.0

        band_1 = fast_get_band(duty_start_1_local, get_time_band_key, band_cache)
        limits_1 = duty_limits.get(band_1)
        if limits_1 is None or prev_duty_hours > limits_1[0]:
            continue

        rest_min_10 = duty_end_1 + TD_10H
        latest_departure = f["STA"] + max_layover_td

        return_candidates = from_groups.get(outstation, [])
        return_stds = from_group_stds.get(outstation, [])
        start_idx = bisect_left(return_stds, rest_min_10)
        end_idx = bisect_left(return_stds, latest_departure)

        for ret in return_candidates[start_idx:end_idx]:
            if ret["To"] != HUB:
                continue

            actual_rest = ret["STD"] - duty_end_1
            has_local_night = actual_rest >= TD_10H and (
                ret["STD"].hour >= 8 or duty_end_1.hour <= 22
            )
            if not _required_rest(prev_duty_hours, actual_rest, has_local_night):
                continue

            if ret["STD"] > latest_departure:
                break  # sorted, so no more valid flights

            duty_start_2 = ret["STD"] - timedelta(minutes=duty_start_buffer)
            duty_start_2_local = ret["STD_local"] - timedelta(minutes=duty_start_buffer)
            duty_end_2 = ret["STA"] + timedelta(minutes=fdp_end_buffer)
            total_duty_2 = (duty_end_2 - duty_start_2).total_seconds() / 3600.0

            band_2 = fast_get_band(duty_start_2_local, get_time_band_key, band_cache)
            limits_2 = duty_limits.get(band_2)
            if limits_2 is None or total_duty_2 > limits_2[0]:
                continue

            if f["flight_id"] in uncovered_ids or ret["flight_id"] in uncovered_ids:
                pairings.append([f, ret])
                metadata.append({
                    "pairing_type": "multi_day",
                    "num_sectors": 2,
                    "num_duties": 2,
                    "is_multi_day": True,
                    "layover_station": outstation,
                    "layover_hours": actual_rest.total_seconds() / 3600,
                })

    return pairings, metadata


# --------------------------------------------------------------------------
# Splicing duties together (turnaround/layover-outbound + layover-inbound)
# --------------------------------------------------------------------------

def valid_sin_connection(arr_flt, dep_flt):
    """Legal same-duty connection at the hub between two flights."""
    required_mgt = arr_flt["MGT_td"] if arr_flt["Eqp"] == dep_flt["Eqp"] else EQP_CHANGE_MGT
    return (dep_flt["STD"] - arr_flt["STA"]) >= required_mgt


def valid_merged_duty(turnaround, outbound, duty_limits, get_time_band_key, band_cache,
                       fdp_end_buffer, duty_end_buffer):
    """Would splicing `outbound` directly onto `turnaround` still fit one FDP window?"""
    sectors = len(turnaround["flights"]) + len(outbound["flights"])
    state = _duty_state(
        sectors, turnaround["duty_start"], turnaround["duty_start_local"],
        outbound["flights"][-1]["STA"], fdp_end_buffer, duty_end_buffer,
        get_time_band_key, band_cache, duty_limits,
    )
    if state is None:
        return False
    total_duty, _band, fdp_limit = state
    return total_duty <= fdp_limit


def valid_rest(duty_a, duty_b, max_layover_td):
    """Legal rest between the end of duty_a and the start of duty_b."""
    duty_end_1 = duty_a["end_time_local"]
    duty_start_2 = duty_b["start_time_local"]
    actual_rest = duty_b["start_time"] - duty_a["end_time"]

    if actual_rest <= timedelta(0) or actual_rest > max_layover_td:
        return False

    has_local_night = actual_rest >= TD_10H and (
        duty_start_2.hour >= 8 or duty_end_1.hour <= 22
    )
    return _required_rest(duty_a["duty_hours"], actual_rest, has_local_night)


def _inbound_by_station(layover_inbound_duties):
    by_station = {}
    for duty in layover_inbound_duties:
        by_station.setdefault(duty["start_station"], []).append(duty)
    return by_station


def _inbound_is_eligible(inbound, outbound, max_layover_td, start_date, end_date, require_multi_leg):
    if require_multi_leg and len(inbound["flights"]) <= 1:
        return False
    if len(inbound["flights"]) >= 2 and not valid_sin_connection(
        inbound["flights"][0], inbound["flights"][1]
    ):
        return False
    if not valid_rest(outbound, inbound, max_layover_td):
        return False
    if any(
        fl["From"] == HUB and not in_date_range(fl, start_date, end_date)
        for fl in inbound["flights"]
    ):
        return False
    return True


def build_combined_pairings(
    layover_outbound_duties, layover_inbound_duties, max_layover_combined, start_date, end_date,
):
    """Layover-outbound duty + a later layover-inbound duty from the same station."""
    combined_pairings = []
    combined_metadata = []
    max_layover_td = timedelta(hours=max_layover_combined)
    inbound_by_station = _inbound_by_station(layover_inbound_duties)

    for outbound in layover_outbound_duties:
        station = outbound["end_station"]
        for inbound in inbound_by_station.get(station, []):
            if not _inbound_is_eligible(
                inbound, outbound, max_layover_td, start_date, end_date, require_multi_leg=True
            ):
                continue

            pairing_flights = outbound["flights"] + inbound["flights"]
            rest_time = inbound["start_time"] - outbound["end_time"]

            combined_pairings.append(pairing_flights)
            combined_metadata.append({
                "pairing_type": "combined_post_layover",
                "num_sectors": len(pairing_flights),
                "num_duties": 2,
                "is_multi_day": True,
                "layover_station": station,
                "layover_hours": rest_time.total_seconds() / 3600,
            })

    return combined_pairings, combined_metadata


def build_pre_layover_combined_pairings(
    turnaround_duties, layover_outbound_duties, layover_inbound_duties, max_layover_combined,
    duty_limits, get_time_band_key, fdp_end_buffer, duty_end_buffer, start_date, end_date,
):
    """A hub turnaround, spliced onto a layover-outbound duty, spliced onto
    a later layover-inbound duty from that station."""
    pairings = []
    metadata = []
    max_layover_td = timedelta(hours=max_layover_combined)
    inbound_by_station = _inbound_by_station(layover_inbound_duties)

    for turnaround in turnaround_duties:
        turnaround_last = turnaround["flights"][-1]

        for outbound in layover_outbound_duties:
            outbound_first = outbound["flights"][0]
            if not valid_sin_connection(turnaround_last, outbound_first):
                continue
            if not valid_merged_duty(
                turnaround, outbound, duty_limits, get_time_band_key, {}, fdp_end_buffer, duty_end_buffer,
            ):
                continue

            station = outbound["end_station"]
            for inbound in inbound_by_station.get(station, []):
                if not _inbound_is_eligible(
                    inbound, outbound, max_layover_td, start_date, end_date, require_multi_leg=False
                ):
                    continue

                pairing = turnaround["flights"] + outbound["flights"] + inbound["flights"]
                rest_time = inbound["start_time"] - outbound["end_time"]

                pairings.append(pairing)
                metadata.append({
                    "pairing_type": "combined_pre_layover",
                    "num_sectors": len(pairing),
                    "num_duties": 3,
                    "is_multi_day": True,
                    "layover_station": station,
                    "layover_hours": rest_time.total_seconds() / 3600,
                })

    return pairings, metadata


# --------------------------------------------------------------------------
# Duty records (used to classify duties as turnaround / layover-outbound /
# layover-inbound before splicing them together)
# --------------------------------------------------------------------------

def _build_duty_record(duty, layover_stations, duty_start_buffer, fdp_end_buffer, duty_end_buffer):
    sectors = len(duty)
    end_buffer = _end_buffer(sectors, fdp_end_buffer, duty_end_buffer)

    duty_start = duty[0]["STD"] - timedelta(minutes=duty_start_buffer)
    duty_end = duty[-1]["STA"] + timedelta(minutes=end_buffer)
    duty_start_local = duty[0]["STD_local"] - timedelta(minutes=duty_start_buffer)
    duty_end_local = duty[-1]["STA_local"] + timedelta(minutes=end_buffer)
    duty_hours = (duty_end - duty_start).total_seconds() / 3600

    start_station = duty[0]["From"]
    end_station = duty[-1]["To"]

    return {
        "flights": duty,
        "start_station": start_station,
        "end_station": end_station,
        "start_time": duty[0]["STD"],
        "end_time": duty[-1]["STA"],
        "start_time_local": duty[0]["STD_local"],
        "end_time_local": duty[-1]["STA_local"],
        "duty_start": duty_start,
        "duty_end": duty_end,
        "duty_start_local": duty_start_local,
        "duty_end_local": duty_end_local,
        "duty_hours": duty_hours,
        "is_turnaround": start_station == HUB and end_station == HUB,
        "is_layover_outbound": start_station == HUB and end_station in layover_stations,
        "is_layover_inbound": start_station in layover_stations and end_station == HUB,
    }


# --------------------------------------------------------------------------
# Top-level orchestration
# --------------------------------------------------------------------------

def build_all_pairings(
    df,
    duty_limits,
    duty_start_buffer,
    fdp_end_buffer,
    duty_end_buffer,
    max_layover_hours, layover_costs_df, max_layover_combined,
    start_date, end_date,
):
    band_cache = {}

    all_flight_records = df.to_dict("records")
    max_duty_regular = max(l for v in duty_limits.values() for l in v)

    from_groups = {}
    all_flight_ids = set()
    start_flights = []

    for f in all_flight_records:
        from_groups.setdefault(f["From"], []).append(f)
        all_flight_ids.add(f["flight_id"])
        if f["From"] == HUB and in_date_range(f, start_date, end_date):
            start_flights.append(f)

    from_group_stds = {}
    for airport in from_groups:
        from_groups[airport].sort(key=lambda x: x["STD"])
        from_group_stds[airport] = [f["STD"] for f in from_groups[airport]]

    start = time.time()
    min_return_hours = build_min_return_hours(from_groups)

    allowed_duty_start_stations = {HUB} | set(layover_costs_df["Station"])
    all_duty_starts = [
        f for f in all_flight_records
        if f["From"] in allowed_duty_start_stations
        and (f["From"] != HUB or in_date_range(f, start_date, end_date))
    ]

    mid1 = time.time()
    print(f"build_min_return_hours: {mid1 - start:.2f}s")
    print("Generating single-day pairings...")

    with Pool(cpu_count()) as pool:
        single_day_worker = partial(
            process_single_start_flight,
            from_groups=from_groups,
            from_group_stds=from_group_stds,
            duty_limits=duty_limits,
            get_time_band_key=get_time_band_key,
            duty_start_buffer=duty_start_buffer,
            fdp_end_buffer=fdp_end_buffer,
            duty_end_buffer=duty_end_buffer,
            min_return_hours=min_return_hours,
            max_duty_regular=max_duty_regular,
        )
        results = pool.map(single_day_worker, start_flights)
        pairings = list(chain.from_iterable(results))
        pairings_metadata = [
            {
                "pairing_type": "single_day",
                "num_sectors": len(p),
                "num_duties": 1,
                "is_multi_day": False,
            }
            for p in pairings
        ]

    with Pool(cpu_count()) as pool:
        duty_worker = partial(
            process_duty_start_flight,
            from_groups=from_groups,
            from_group_stds=from_group_stds,
            duty_limits=duty_limits,
            get_time_band_key=get_time_band_key,
            duty_start_buffer=duty_start_buffer,
            fdp_end_buffer=fdp_end_buffer,
            duty_end_buffer=duty_end_buffer,
            min_return_hours=min_return_hours,
            max_duty_regular=max_duty_regular,
        )
        duty_results = pool.map(duty_worker, all_duty_starts)
        all_duties = list(chain.from_iterable(duty_results))

    print(f"Total single-day pairings: {len(pairings)}")
    mid2 = time.time()
    print(f"Single-day pairings: {mid2 - mid1:.2f}s")

    covered_ids = {f["flight_id"] for p in pairings for f in p}
    uncovered_ids = all_flight_ids - covered_ids
    print(f"Flights requiring multi-day pairing: {len(uncovered_ids)}")

    layover_stations = set(layover_costs_df["Station"])

    multi_day_pairings, multi_day_metadata = find_two_day_pairings_optimized(
        from_groups=from_groups,
        from_group_stds=from_group_stds,
        uncovered_ids=uncovered_ids,
        duty_limits=duty_limits,
        get_time_band_key=get_time_band_key,
        band_cache=band_cache,
        duty_start_buffer=duty_start_buffer,
        fdp_end_buffer=fdp_end_buffer,
        max_layover_hours=max_layover_hours,
        layover_stations=layover_stations,
        start_date=start_date,
        end_date=end_date,
    )
    end = time.time()
    print(f"Multi-day pairings: {end - mid2:.2f}s")
    print(f"Total: {end - start:.2f}s")
    print(f"Total multi-day pairings: {len(multi_day_pairings)}")

    pairings.extend(multi_day_pairings)
    pairings_metadata.extend(multi_day_metadata)

    all_duty_records = [
        _build_duty_record(d, layover_stations, duty_start_buffer, fdp_end_buffer, duty_end_buffer)
        for d in all_duties
    ]
    turnaround_duties = [d for d in all_duty_records if d["is_turnaround"]]
    layover_outbound_duties = [d for d in all_duty_records if d["is_layover_outbound"]]
    layover_inbound_duties = [d for d in all_duty_records if d["is_layover_inbound"]]

    combined_pairings, combined_metadata = build_combined_pairings(
        layover_outbound_duties, layover_inbound_duties, max_layover_combined,
        start_date=start_date, end_date=end_date,
    )
    pre_pairings, pre_metadata = build_pre_layover_combined_pairings(
        turnaround_duties, layover_outbound_duties, layover_inbound_duties, max_layover_combined,
        duty_limits, get_time_band_key,
        fdp_end_buffer=fdp_end_buffer, duty_end_buffer=duty_end_buffer,
        start_date=start_date, end_date=end_date,
    )

    pairings.extend(combined_pairings)
    pairings_metadata.extend(combined_metadata)
    pairings.extend(pre_pairings)
    pairings_metadata.extend(pre_metadata)

    return pairings, pairings_metadata