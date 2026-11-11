"""Make the plugin root importable as ``triage`` without installing it."""

from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = "triage"

if PKG not in sys.modules:
    package = types.ModuleType(PKG)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    package.__package__ = PKG
    sys.modules[PKG] = package

DEMO_SKILLS = ROOT / "examples" / "demo-skills"
