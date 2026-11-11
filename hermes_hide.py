"""Hide Hermes' own folder skills from Hermes, so Triage is the only way in.

Hermes discovers skills in its own folders (``~/.hermes/skills`` and
``skills.external_dirs``) and lists them in its ``/`` menu and skills index.
Hermes' setting ``skills.disabled`` hides a skill from all of that without
touching its files. With ``hide_hermes_skills`` on (default), Triage keeps that
list in step with the Hermes-folder skills it indexes: a skill installed from
the marketplace or created by the model is hidden as soon as Triage has it,
and Triage finds, loads and serves it instead.

Never hidden: ``hermes-agent`` (Hermes refuses anyway) and the always-load
names (global setting). Skills in Hermes folders that Triage does not index
are left alone, so nothing becomes unreachable.

Every name Triage adds is recorded in ``hidden_by_triage.json`` in the
plugin's data folder. Turning the setting off, or ``hermes triage restore``
(run it before uninstalling Triage), removes exactly those names again;
names you disabled yourself stay disabled.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("triage")

RECORD = "hidden_by_triage.json"
ALWAYS_VISIBLE = frozenset({"hermes-agent"})


def _record_path(db_path: Path) -> Path:
    return Path(db_path).parent / RECORD


def _read_record(db_path: Path) -> set[str]:
    try:
        return set(json.loads(_record_path(db_path).read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return set()


def _write_record(db_path: Path, names: set[str]) -> None:
    path = _record_path(db_path)
    if names:
        path.write_text(json.dumps(sorted(names), indent=0), encoding="utf-8")
    elif path.exists():
        path.unlink()


def _disabled_list(config: dict) -> list[str]:
    from agent.skill_utils import parse_config_string_list  # handles JSON-string lists

    skills = config.get("skills") or {}
    return [str(name) for name in parse_config_string_list(skills.get("disabled") or [])]


def _save_disabled(names: list[str]) -> None:
    from hermes_cli.config import load_config, save_config

    config = load_config() or {}
    config.setdefault("skills", {})["disabled"] = names
    save_config(config, merge_existing=True)


def hermes_folder_skill_names(store) -> set[str]:
    """Names Triage indexes from Hermes' own skill folders."""
    from .catalog_link import _inside_any, hermes_skill_roots

    roots = hermes_skill_roots()
    names: set[str] = set()
    for root in store.folder_roots():
        if _inside_any(Path(root), roots) or any(
            os.path.normcase(str(Path(root).resolve())) == os.path.normcase(str(r.resolve())) for r in roots
        ):
            names.update(name for name, _fingerprint in store.folder_manifest(root).values())
    return names


def sync_hidden(store, db_path: Path, *, enabled: bool, keep_visible: set[str] | None = None) -> str:
    """Bring ``skills.disabled`` in line. Returns a log line, or '' when nothing changed."""
    try:
        from hermes_cli.config import load_config
    except Exception:
        return ""  # not running inside Hermes (tests, CLI tools)
    config = load_config() or {}
    disabled = _disabled_list(config)
    recorded = _read_record(db_path)
    if not enabled:
        if not recorded:
            return ""
        kept = [name for name in disabled if name not in recorded]
        _save_disabled(kept)
        _write_record(db_path, set())
        return f"restored {len(disabled) - len(kept)} Hermes skill(s) to Hermes' own menu"
    visible = {name.casefold() for name in (keep_visible or set()) | ALWAYS_VISIBLE}
    want = {name for name in hermes_folder_skill_names(store) if name.casefold() not in visible}
    current = set(disabled)
    if not recorded:
        # First run: names already disabled that Triage now serves count as ours.
        recorded = current & want
    add = sorted(want - current)
    release = sorted(name for name in recorded if name not in want and name in current)
    if not add and not release and recorded == (recorded & (want | current)):
        _write_record(db_path, recorded | (want & current))
        return ""
    new_list = [name for name in disabled if name not in release] + add
    _save_disabled(new_list)
    _write_record(db_path, (recorded - set(release)) | set(add) | (want & current))
    return f"Hermes skills.disabled: +{len(add)} hidden (Triage serves them), -{len(release)} given back"


def restore_all(db_path: Path) -> str:
    """Give back every skill Triage hid (``hermes triage restore``)."""
    return sync_hidden(None, db_path, enabled=False) or "Nothing to restore: Triage has not hidden any Hermes skill."
