import pandas as pd

from datetime import timedelta, time as dt_time  # ADDED: dt_time import for time_to_minutes()
from typing import Optional


def eqp_in_group(row_eqp, group_eqp, AIRCRAFT_GROUPS):
    return row_eqp in AIRCRAFT_GROUPS.get(group_eqp, set())


def parse_hmm_to_minutes(hmm):
    # UNCHANGED — still used for parsing the 'MGT' duration column (e.g. '130' -> 90min)
    if pd.isnull(hmm):
        return None
    hmm = str(hmm).lower().replace(' ', '')
    if 'h' in hmm:
        h, m = 0, 0
        parts = hmm.split('h')
        h = int(parts[0]) if parts[0].isdigit() else 0
        if 'm' in parts[1]:
            m = int(parts[1].replace('m', ''))
        return h * 60 + m
    if hmm.isdigit():
        hmm = hmm.zfill(3)
        if len(hmm) == 3:
            h = int(hmm[0])
            m = int(hmm[1:])
            return h * 60 + m
        elif len(hmm) == 2:
            return int(hmm)
        else:
            return None
    return None


# ===== ADDED FUNCTION =====
# Parses the *clock-time* values in 'Arr Start' / 'Arr End' (e.g. 2100 -> 21:00, 530 -> 05:30).
# Kept separate from parse_hmm_to_minutes() because that one parses MGT *durations*, not clock times —
# they use the same "digits" shape but mean different things.
def parse_clock_hhmm_to_minutes(value):
    if pd.isnull(value):
        return None
    s = str(value).strip()
    if s == '' or s.lower() == 'nan':
        return None
    try:
        s = str(int(float(s)))
    except ValueError:
        return None
    s = s.zfill(4)
    h = int(s[:2])
    m = int(s[2:])
    if h > 23 or m > 59:
        return None
    return h * 60 + m


# ===== ADDED FUNCTION =====
# Converts a row's scheduled arrival time (whatever type it is in your df — Timestamp,
# datetime.time, 'HH:MM' string, or HHMM number) into minutes-since-midnight, so it can
# be compared against the parsed Arr Start / Arr End window.
def time_to_minutes(t):
    if t is None:
        return None
    if isinstance(t, float) and pd.isnull(t):
        return None
    if isinstance(t, pd.Timestamp):
        return t.hour * 60 + t.minute
    if isinstance(t, dt_time):
        return t.hour * 60 + t.minute
    if hasattr(t, 'hour') and hasattr(t, 'minute'):
        return t.hour * 60 + t.minute
    s = str(t).strip()
    if ':' in s:
        parts = s.split(':')
        try:
            return int(parts[0]) * 60 + int(parts[1])
        except ValueError:
            return None
    return parse_clock_hhmm_to_minutes(s)


# ===== ADDED FUNCTION =====
# True if t_min falls inside [start_min, end_min]. Also handles windows that wrap past
# midnight (e.g. 2300-0100), even though none of your current rows need that case.
def _time_in_window(t_min, start_min, end_min):
    if t_min is None or start_min is None or end_min is None:
        return False
    if start_min <= end_min:
        return start_min <= t_min <= end_min
    return t_min >= start_min or t_min <= end_min


def resolve_mgt_minutes(
    row_eqp: str,
    airport: str,
    country: str,
    mgt_df: pd.DataFrame,
    AIRCRAFT_GROUPS,
    arr_time_minutes: Optional[int] = None,  # ADDED PARAMETER: flight's local STA in minutes-since-midnight
) -> Optional[int]:
    row_eqp = str(row_eqp).strip().upper()
    airport = str(airport).strip().upper()
    country = str(country).strip().upper()

    mgt = mgt_df
    mgt_eqp = mgt['Eqp'].astype(str).str.strip().str.upper()
    via = mgt['Via Arpt'].fillna('').astype(str).str.strip().str.upper()
    via_lvl = mgt['Via Lvl'].fillna('').astype(str).str.strip().str.upper()
    eqp_lvl = mgt['Eqp Lvl'].fillna('').astype(str).str.strip().str.upper()

    # ===== ADDED BLOCK =====
    # Pre-parse the Arr Start / Arr End columns once, and precompute which rows have a
    # window at all, and which of those windows contain this flight's arrival time.
    arr_start_min = mgt['Arr Start'].apply(parse_clock_hhmm_to_minutes)
    arr_end_min = mgt['Arr End'].apply(parse_clock_hhmm_to_minutes)
    has_window = arr_start_min.notna() & arr_end_min.notna()

    if arr_time_minutes is not None:
        in_window = pd.Series(
            [
                _time_in_window(arr_time_minutes, s, e)
                for s, e in zip(arr_start_min, arr_end_min)
            ],
            index=mgt.index,
        )
    else:
        in_window = pd.Series(False, index=mgt.index)
    # ===== END ADDED BLOCK =====

    def _first(mask):
        # UNCHANGED
        if not mask.any():
            return None
        return parse_hmm_to_minutes(mgt.loc[mask].iloc[0]['MGT'])

    # ===== ADDED FUNCTION (replaces the old bare `_first(mask)` call at each level) =====
    # For a given level's base mask (e.g. "Subfleet + Country" rows), first look for a rule
    # whose Arr Start/Arr End window contains the flight's arrival time. Only if there's no
    # windowed match does it fall back to that level's no-window default row.
    def _match(base_mask):
        v = _first(base_mask & has_window & in_window)
        if v is not None:
            return v
        v = _first(base_mask & ~has_window)
        if v is not None:
            return v
        return None
    # ===== END ADDED FUNCTION =====

    # BELOW: each of the 6 levels is unchanged in *what* it matches on, but now calls
    # _match(base) instead of _first(mask) directly, and the mask is stored as `base`
    # first so it can be reused for both the windowed and default lookup inside _match().

    # 1) Subfleet + Airport
    base = (eqp_lvl == 'SUBFLEET') & (mgt_eqp == row_eqp) & (via_lvl == 'AIRPORT') & (via == airport)
    v = _match(base)  # CHANGED: was _first(base)
    if v is not None:
        return v

    # 2) Aircraft Group + Airport
    base = (
        (eqp_lvl == 'AIRCRAFT GROUP') &
        (via_lvl == 'AIRPORT') &
        (via == airport) &
        (mgt_eqp.apply(lambda g: eqp_in_group(row_eqp, g, AIRCRAFT_GROUPS)))
    )
    v = _match(base)  # CHANGED: was _first(base)
    if v is not None:
        return v
    
    # 3) Subfleet + Country
    base = (eqp_lvl == 'SUBFLEET') & (mgt_eqp == row_eqp) & (via_lvl == 'COUNTRY') & (via == country)
    v = _match(base)  # CHANGED: was _first(base)
    if v is not None:
        return v

    # 4) Aircraft Group + Country
    base = (
        (eqp_lvl == 'AIRCRAFT GROUP') &
        (via_lvl == 'COUNTRY') &
        (via == country) &
        (mgt_eqp.apply(lambda g: eqp_in_group(row_eqp, g, AIRCRAFT_GROUPS)))
    )
    v = _match(base)  # CHANGED: was _first(base)
    if v is not None:
        return v

    # 5) Subfleet Default
    base = (eqp_lvl == 'SUBFLEET') & (mgt_eqp == row_eqp) & (via == '')
    v = _match(base)  # CHANGED: was _first(base)
    if v is not None:
        return v

    # 6) Aircraft Group Default
    base = (
        (eqp_lvl == 'AIRCRAFT GROUP') &
        (via == '') &
        (mgt_eqp.apply(lambda g: eqp_in_group(row_eqp, g, AIRCRAFT_GROUPS)))
    )
    v = _match(base)  # CHANGED: was _first(base)
    if v is not None:
        return v

    return None


def build_cross_eqp_mgt_lookup_from_resolver(
    mgt_df,
    airport_to_country,
    subtypes,
    AIRCRAFT_GROUPS,
    default_minutes=60,
):
    # NOTE: no per-flight arrival time exists in this generic (airport, eqp) lookup,
    # so arr_time_minutes is passed as None below -> windowed rules are skipped here
    # and only default rules are used, same as the function's original behavior.
    lookup = {}
    fallback_rows = []
    airports = set(airport_to_country.keys())
    airports |= set(mgt_df['Via Arpt'].dropna().astype(str).str.upper())

    for airport in airports:
        country = airport_to_country.get(airport, '')
        for eqp in subtypes:
            minutes = resolve_mgt_minutes(
                row_eqp=eqp,
                airport=airport,
                country=country,
                mgt_df=mgt_df,
                AIRCRAFT_GROUPS=AIRCRAFT_GROUPS,
                arr_time_minutes=None,  # ADDED ARGUMENT (explicit None to keep old behavior)
            )
            if minutes is None:
                fallback_rows.append({
                    "airport": airport,
                    "country": country,
                    "eqp": eqp,
                    "fallback_minutes": default_minutes,
                })
                minutes = default_minutes

            lookup[(airport, eqp)] = timedelta(minutes=int(minutes))

    return lookup, fallback_rows


def apply_mgt_processing(
    df,
    MGT,
    AIRCRAFT_GROUPS,
    airport_to_country,
    subtypes,
    sta_col: str = 'STA',  # ADDED PARAMETER: which df column holds local scheduled arrival time
):
    # CHANGED: 'Arr Start' and 'Arr End' removed from this drop list — they're now needed
    # for window matching inside resolve_mgt_minutes(), so we keep them on MGT.
    MGT = MGT.drop(columns=[
        'Type', 'Cxr', 'Up Lvl', 'Up Arpt', 'Dn Lvl', 'Dn Arpt',
        'Up Svc', 'Dn Svc', 'Up Note', 'Dn Note', 'Base',
    ])

    # ===== ADDED LINE =====
    # Precompute each flight's local arrival time as minutes-since-midnight, once, for use below.
    arr_time_minutes = df[sta_col].apply(time_to_minutes)

    df['MGT'] = df.apply(
        lambda r: resolve_mgt_minutes(
            row_eqp=r['Eqp'],
            airport=r['To'],
            country=r['Arvl Ctry'],
            mgt_df=MGT,
            AIRCRAFT_GROUPS=AIRCRAFT_GROUPS,
            arr_time_minutes=arr_time_minutes.loc[r.name],  # ADDED ARGUMENT
        ),
        axis=1
    )
    df["RT_sin"] = df["STA_sin"] + pd.to_timedelta(df["MGT"], unit="m")
    df['Eqp'] = df['Eqp'].astype(str)
    df = df.rename(columns={'STD': 'STD_local', 'STA': 'STA_local'})
    df = df.rename(columns={'STD_sin': 'STD', 'STA_sin': 'STA', 'RT_sin': 'RT'})
    df["MGT_td"] = pd.to_timedelta(df["MGT"], unit="m")
    cross_eqp_mgt_lookup, fallback_rows = build_cross_eqp_mgt_lookup_from_resolver(
        mgt_df=MGT,
        airport_to_country=airport_to_country,
        subtypes=subtypes,
        AIRCRAFT_GROUPS=AIRCRAFT_GROUPS,
    )
    return df, cross_eqp_mgt_lookup, fallback_rows