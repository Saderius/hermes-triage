"""Always-load skills: a fixed list that enters a session on its first prompt.

Two sources, merged in this order (duplicates dropped, case-insensitive):

1. Global: the ``always_load`` plugin setting (a list, or a comma/newline
   separated string) under ``plugins.entries.triage.settings``.
2. Project: ``always_load`` in ``.hermes/triage.yaml`` (or ``.yml``),
   found by walking up from the session's working directory.

The list is applied on a session's first prompt and again on the first prompt
after a compaction (the bodies left the window with the compacted turns).
Everything else in the catalog still goes through the judge.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

PROJECT_FILES = (".hermes/triage.yaml", ".hermes/triage.yml")
_MAX_WALK = 12


def parse_names(value: Any) -> list[str]:
    """Normalize a setting value into skill names, order kept."""
    if value is None:
        return []
    if isinstance(value, str):
        items: Iterable[Any] = re.split(r"[,\n]", value)
    elif isinstance(value, (list, tuple, set)):
        items = value
    else:
        return []
    out: list[str] = []
    for item in items:
        name = str(item or "").strip().strip("'\"").strip()
        if name and not name.startswith("#"):
            out.append(name)
    return out


def merge_names(*groups: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for group in groups:
        for name in group:
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(name)
    return out


def find_project_file(cwd: str | Path | None) -> Path | None:
    if not cwd:
        return None
    try:
        here = Path(cwd).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    for _ in range(_MAX_WALK):
        for rel in PROJECT_FILES:
            candidate = here / rel
            if candidate.is_file():
                return candidate
        if here.parent == here:
            break
        here = here.parent
    return None


def project_names(cwd: str | Path | None) -> tuple[list[str], Path | None]:
    path = find_project_file(cwd)
    if path is None:
        return [], None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return [], path
    return parse_names(_always_load_value(text)), path


def _always_load_value(text: str) -> Any:
    try:
        import yaml  # Hermes ships PyYAML; the fallback keeps the plugin stdlib-only.

        data = yaml.safe_load(text) or {}
        if isinstance(data, dict):
            return data.get("always_load")
        return None
    except ImportError:
        return _fallback_always_load(text)
    except Exception:
        return _fallback_always_load(text)


def _fallback_always_load(text: str) -> Any:
    """Read ``always_load: [a, b]`` or a ``- name`` block without PyYAML."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^always_load\s*:\s*(.*)$", line)
        if not match:
            continue
        rest = match.group(1).split("#", 1)[0].strip()
        if rest.startswith("["):
            return rest.strip("[]")
        if rest:
            return rest
        items: list[str] = []
        for follow in lines[index + 1:]:
            item = re.match(r"^\s+-\s*(.+?)\s*$", follow)
            if item:
                items.append(item.group(1).split("#", 1)[0].strip())
                continue
            if follow.strip() and not follow.lstrip().startswith("#"):
                break
        return items
    return None
