"""Standalone entry so setup works before Hermes imports the plugin package.

    python run.py migrate --from ~/.hermes/skills
    python run.py migrate --from examples/demo-skills --apply-config --config /tmp/config.yaml
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PKG = "triage"


def _install_package() -> None:
    existing = sys.modules.get(PKG)
    if existing is not None and getattr(existing, "__path__", None):
        return
    package = types.ModuleType(PKG)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    package.__package__ = PKG
    sys.modules[PKG] = package


def main() -> None:
    _install_package()
    cli = importlib.import_module(f"{PKG}.cli")
    raise SystemExit(cli.main())


if __name__ == "__main__":
    main()
