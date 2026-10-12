"""Compatibility shim forwarding to the supplied source in originals/pairings_builder.py."""
from pathlib import Path as _Path
import runpy as _runpy
_source = _Path(__file__).resolve().parent / "originals/pairings_builder.py"
_namespace = _runpy.run_path(str(_source), run_name="_supplied_pairings_builder_py")
globals().update({k: v for k, v in _namespace.items() if not (k.startswith("__") and k.endswith("__"))})
