"""Backward-compatible root entry point; implementation lives in scripts/prep_inputs.py."""
from pathlib import Path as _Path
import runpy as _runpy
_target = _Path(__file__).resolve().parent / "scripts" / "prep_inputs.py"
_namespace = _runpy.run_path(str(_target), run_name="__main__" if __name__ == "__main__" else "_compat_prep_inputs")
globals().update({k: v for k, v in _namespace.items() if not (k.startswith("__") and k.endswith("__"))})
