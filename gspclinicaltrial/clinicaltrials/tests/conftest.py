"""Pytest configuration — ensure clinicaltrials/ is on sys.path."""

import pathlib
import sys

_PROJECT_ROOT = str(pathlib.Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
