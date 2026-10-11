#!/usr/bin/env python3
"""Run once. Put your 14 original files in ./originals/ then: python setup_workspace.py
Copies them to the project root (where the scripts look) and creates input/ + the de-duplicated schedule."""
import shutil, subprocess, sys
from pathlib import Path
R=Path(__file__).resolve().parent; O=R/"originals"
need=["MGT.py","MGT.xlsx","cost_parameters.csv","fdp_rules.csv","low_kgp","pairing_report.xlsx","pairings_builder.py","revenue.xlsx","schedule_kgp.xlsx"]
miss=[f for f in need if not (O/f).exists()]
if miss: sys.exit(f"Missing in originals/: {miss}")
(R/"input").mkdir(exist_ok=True)
for f in need: shutil.copy2(O/f,R/f)
shutil.copy2(O/"pairing_report.xlsx",R/"input"/"pairing_report.xlsx")
subprocess.check_call([sys.executable,str(R/"prep_inputs.py")])
print("Workspace ready.")
