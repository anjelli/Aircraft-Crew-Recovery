"""Compatibility shim forwarding to the supplied source in originals/MGT.py."""
from pathlib import Path as _Path
import runpy as _runpy
_source = _Path(__file__).resolve().parent / "originals/MGT.py"
_namespace = _runpy.run_path(str(_source), run_name="_supplied_MGT_py")
globals().update({k: v for k, v in _namespace.items() if not (k.startswith("__") and k.endsWith("__"))})
