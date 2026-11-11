"""Find old-name Triage installs that make Hermes see two copies.

The plugin was called ``hermes-skill-judge`` before it was renamed ``triage``.
A leftover ``plugins/hermes-skill-judge`` or ``desktop-plugins/hermes-skill-judge``
(or an underscore ``hermes_skill_judge`` alias) loads beside ``triage`` and
registers the same hooks twice.

    python scripts/sanitize_desktop_plugin.py
    python scripts/sanitize_desktop_plugin.py --yes

``--yes`` removes the old-name entries under ``plugins/`` and ``desktop-plugins/``.
A junction or symlink is unlinked, never followed. ``plugin-data`` is never
touched: Triage keeps reading ``plugin-data/hermes-skill-judge`` until
``plugin-data/triage`` exists.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

OLD_NAMES = ("hermes-skill-judge", "hermes_skill_judge")


def hermes_home() -> Path:
    override = os.environ.get("HERMES_HOME")
    if override:
        return Path(override).expanduser()
    local = os.environ.get("LOCALAPPDATA")
    if sys.platform == "win32" and local and (Path(local) / "hermes").exists():
        return Path(local) / "hermes"
    return Path.home() / ".hermes"


def _is_link(path: Path) -> bool:
    try:
        return path.is_symlink() or bool(getattr(os.lstat(path), "st_reparse_tag", 0))
    except OSError:
        return False


def _remove(path: Path) -> None:
    if _is_link(path):
        os.rmdir(path) if path.is_dir() else path.unlink()
    else:
        shutil.rmtree(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="Remove the old-name entries")
    parser.add_argument("--home", type=Path, default=None)
    args = parser.parse_args(argv)
    home = args.home or hermes_home()
    found = [
        home / root / name
        for root in ("plugins", "desktop-plugins")
        for name in OLD_NAMES
        if os.path.lexists(home / root / name)
    ]
    if not found:
        print(f"No old-name installs under {home}.")
        return 0
    for path in found:
        kind = "link" if _is_link(path) else "folder"
        print(f"{'Removing' if args.yes else 'Would remove'} {kind} {path}")
        if args.yes:
            _remove(path)
    if not args.yes:
        print("Re-run with --yes.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
