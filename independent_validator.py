"""Compatibility shim; the implementation lives in src/crew_recovery/independent_validator.py."""
from pathlib import Path as _Path
import sys as _sys
_SRC = _Path(__file__).resolve().parent / "src"
if str(_SRC) not in _sys.path:
    _sys.path.insert(0, str(_SRC))
from crew_recovery import independent_validator as _implementation
globals().update({k: v for k, v in vars(_implementation).items() if not (k.startswith("__") and k.endswith("__"))})
