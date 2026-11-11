"""Locations for the plugin store and the Hermes profile this process should read."""

from __future__ import annotations

import os
from pathlib import Path

PLUGIN_NAME = "triage"
# Earlier install id. Its plugin-data folder is used while the new one has no
# skills (the catalog stores absolute mirror paths; see docs for the move).
LEGACY_NAMES = ("hermes-skill-judge",)


def hermes_home() -> Path:
    """Active Hermes profile directory.

    ``HERMES_HOME`` wins. Otherwise ``~/.hermes``.
    """
    override = os.environ.get("HERMES_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".hermes"


def default_skills_dir() -> Path:
    return hermes_home() / "skills"


def default_config_path() -> Path:
    return hermes_home() / "config.yaml"


def default_data_dir() -> Path:
    """Durable plugin data, outside the install tree.

    Prefers Hermes ``plugin_data_dir`` when the host is importable, so updates
    and ``hermes plugins remove`` do not wipe the catalog. Falls back to
    ``$HERMES_HOME/plugin-data/triage``. An existing
    ``plugin-data/hermes-skill-judge`` (the old install id) wins while the new
    folder does not exist, so a rename keeps the catalog and its mirrors.
    """
    try:
        from plugins.plugin_storage import plugin_data_dir

        current = Path(plugin_data_dir(PLUGIN_NAME))
    except Exception:
        current = hermes_home() / "plugin-data" / PLUGIN_NAME
    # Hermes' plugin_data_dir() creates the folder, so "exists" says nothing:
    # an empty new catalog must not shadow a populated legacy one.
    if _skill_count(current / "catalog.db") == 0:
        for legacy in LEGACY_NAMES:
            candidate = current.parent / legacy
            if _skill_count(candidate / "catalog.db") > 0:
                return candidate
    return current


def _skill_count(db: Path) -> int:
    if not db.exists():
        return 0
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return 0
    try:
        return int(conn.execute("SELECT count(*) FROM skills").fetchone()[0])
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def default_db_path() -> Path:
    return default_data_dir() / "catalog.db"
