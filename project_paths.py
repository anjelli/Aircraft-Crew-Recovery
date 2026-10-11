"""Canonical, deterministic paths for source and prepared project inputs.

Source-of-truth contract:
- originals/: supplied source artifacts; code must never search around for substitutes.
- input/: explicitly prepared inputs used by experiments.
- project root: Python entry points and generated outputs only.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ORIGINALS_DIR = ROOT / "originals"
INPUT_DIR = ROOT / "input"


def _require_file(directory: Path, filename: str, kind: str) -> Path:
    # Reject path traversal and accidental nested/alternate file names.
    if Path(filename).name != filename or filename in {"", ".", ".."}:
        raise ValueError(f"{kind} filename must be a single file name, got {filename!r}")
    path = directory / filename
    if not path.is_file():
        raise FileNotFoundError(
            f"Required {kind} file not found: {path}. "
            f"Place the canonical file in {directory}."
        )
    return path


def original_file(filename: str) -> Path:
    """Resolve a supplied source artifact from originals/ only."""
    return _require_file(ORIGINALS_DIR, filename, "source")


def input_file(filename: str) -> Path:
    """Resolve a prepared runtime input from input/ only."""
    return _require_file(INPUT_DIR, filename, "prepared input")
