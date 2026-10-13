from __future__ import annotations

import pandas as pd
import pytest

from crew_recovery.aircraft_line_inputs import load_320_line_inventory


def write_inputs(tmp_path):
    lines = tmp_path / "low_kgp"
    lines.write_text(
        "AIRFLITE LINES OF WORK,TR_base_KGP,84,25Oct26,28Mar27,Local\n"
        "Line Num,Leg Seq num,Subfleet Line Num,Equip,A/C Own,Aln,Flt Num,Ops Suf,Grnd Evnt Typ,Service Type,AC Config,Dept Sta,Dept Tr,Dept Date,Shift Flt Date,Dept Time,MCT Status,Arvl Sta,Arvl Tr,Arvl Date,Arvl Time,TURN DEPENDABILITY BUFFER,TOTAL MGT,TURN TOW TIME,REASON CODE\n"
        "1,1,1,320,TR,TR,101,,,J,Y180,SIN,,25Oct26,25Oct26,0800,II,KUL,,25Oct26,0900,0,0,0,\n"
        "2,1,2,320,TR,TR,201,,,J,Y180,KUL,,25Oct26,25Oct26,0830,II,SIN,,25Oct26,0930,0,0,0,\n",
        encoding="utf-8",
    )
    schedule = tmp_path / "schedule.xlsx"
    pd.DataFrame([
        {"Day": "2026-10-25", "Flt Num": 101, "From": "SIN", "To": "KUL", "STD": "2026-10-25 08:00", "STA": "2026-10-25 09:00", "Eqp": "320"},
        {"Day": "2026-10-25", "Flt Num": 201, "From": "KUL", "To": "SIN", "STD": "2026-10-25 08:30", "STA": "2026-10-25 09:30", "Eqp": "320"},
    ]).to_excel(schedule, index=False)
    return lines, schedule


def test_loads_resources_and_exact_flight_mappings(tmp_path):
    lines, schedule = write_inputs(tmp_path)
    aircraft, assignments = load_320_line_inventory(lines, schedule, line_numbers=(1, 2))
    assert [a.tail_id for a in aircraft] == ["LINE_1", "LINE_2"]
    assert [(a.equipment, a.start_station) for a in aircraft] == [("320", "SIN"), ("320", "KUL")]
    assert assignments["flight_id"].tolist() == ["101_20261025T080000", "201_20261025T083000"]
    assert assignments["provisional_aircraft_id"].tolist() == ["LINE_1", "LINE_2"]


def test_fails_closed_if_schedule_flight_is_missing(tmp_path):
    lines, schedule = write_inputs(tmp_path)
    pd.read_excel(schedule).iloc[:1].to_excel(schedule, index=False)
    with pytest.raises(ValueError, match="reconciliation failed"):
        load_320_line_inventory(lines, schedule, line_numbers=(1, 2))


def test_fails_closed_on_equipment_mismatch(tmp_path):
    lines, schedule = write_inputs(tmp_path)
    df = pd.read_excel(schedule)
    df.loc[0, "Eqp"] = "32N"
    df.to_excel(schedule, index=False)
    with pytest.raises(ValueError, match="reconciliation failed"):
        load_320_line_inventory(lines, schedule, line_numbers=(1, 2))
