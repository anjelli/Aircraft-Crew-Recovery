from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from scripts import run_joint_recovery_horizons as horizon_runner


def test_horizon_summary_preserves_objective_and_reconciliation_audits(monkeypatch, tmp_path):
    reconciliation = {
        "status": "PASS",
        "flights_compared": 12,
        "scenario_modified_flights": 2,
        "consistency_failures": 0,
        "output_file": "schedule_reconciliation.csv",
    }
    objective_ledger = {
        "status": "PARTIAL_PROXY",
        "implemented_stages": [{"name": "cancellation_count", "value": 3}],
    }

    def fake_run(command, *, cwd, text, capture_output):
        output_dir = Path(command[command.index("--output-dir") + 1])
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "summary.json").write_text(
            json.dumps({
                "status": "PROTOTYPE_OPTIMAL_OVER_SUPPLIED_COLUMNS",
                "active_flights": 12,
                "operated_flights": 9,
                "cancelled_flights": 3,
                "unresolved_mgt_flights": 0,
                "aircraft_violations": [],
                "crew_coverage_errors": [],
                "operational_acceptance": False,
                "objective_ledger": objective_ledger,
                "schedule_reconciliation": reconciliation,
            }),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(horizon_runner.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_joint_recovery_horizons.py",
            "--horizons",
            "1W",
            "--output-root",
            str(tmp_path),
        ],
    )

    assert horizon_runner.main() == 0
    aggregate_path = tmp_path / "horizon_summary.json"
    aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))

    assert len(aggregate["horizons"]) == 1
    horizon = aggregate["horizons"][0]
    assert horizon["schedule_reconciliation"] == reconciliation
    assert horizon["objective_ledger"] == objective_ledger
    assert horizon["operated_flights"] == 9
    assert horizon["operational_acceptance"] is False
