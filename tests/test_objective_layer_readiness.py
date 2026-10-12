from pathlib import Path
from objective_layer_readiness import baseline_breakdown,calendar_days,parse_embedded_summary
def test_true_span_is_155_not_166():
 assert calendar_days("2026-10-25","2026-10-31")==7
 assert calendar_days("2026-10-25","2027-03-28")==155
def test_saved_coverage_objective_decomposes(tmp_path:Path):
 p=tmp_path/"logs";p.mkdir()
 (p/"mip_1W.log").write_text('log [1W] runtime=0.02s\n{"Horizon":"1 week","Start Date":"2026-10-25","End Date Used":"2026-10-31","Active Flights":811,"Selected Pairings":325,"Flights Covered After MIP":728,"Flights Uncovered After MIP":83,"Objective":67721.0,"Solver Status":"OPTIMAL"}\n',encoding="utf-8")
 df,s=baseline_breakdown(tmp_path);r=df.loc[df["Horizon Tag"]=="1W"].iloc[0]
 assert r["M Factor (active flights + 1)"]==812 and r["Uncovered Slack Objective Component"]==67396
 assert r["Selected Pairing Count Objective Component"]==325 and r["Recomputed Coverage-Only Objective"]==67721
 assert bool(r["Objective Decomposition Matches"]) is True and r["True Calendar Days Inclusive"]==7
def test_summary_embedded_after_log_line():
 assert parse_embedded_summary('log line\n{ "count": 3 }\n')=={"count":3}
