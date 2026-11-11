"""How the catalog reaches Hermes: skill folders in, `/triage:<name>` out.

* **Skill folders** (plugin setting ``skill_folders``): imported into the
  catalog on gateway start and on ``/triage sync``. Import copies each skill
  tree into the plugin's mirror, so the folder can move later.
* **Registration**: every catalog skill is registered with Hermes as a plugin
  skill, ``/triage:<name>``. Hermes' own ``/`` menu and its stacked loader
  (``/a /b do X``, up to 5) then work for the whole catalog. The skills index
  Hermes builds from these registrations is stripped from every request.
* **Attached skills**: when a message is sent, Hermes expands the leading
  tokens into skill blocks. ``invoked_skills`` reads which skills that
  message carried; ``leading_tokens`` covers tokens Hermes left as text.
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path
from typing import Iterable

logger = logging.getLogger("triage")

NAMESPACE = "triage"
_VALID = re.compile(r"^[A-Za-z0-9_-]+$")
_SINGLE = re.compile(r'\[IMPORTANT: The user has invoked the "([^"]+)" skill')
_STACKED = re.compile(r"^Skills loaded: (.+)$", re.MULTILINE)
_TOKEN = re.compile(r"^/(?:" + NAMESPACE + r":)?([A-Za-z0-9_-]+)$")


def valid_name(name: str) -> bool:
    return bool(name) and bool(_VALID.match(name))


def slash_token(name: str) -> str:
    """The token the composer inserts for a catalog skill ('' when Hermes can't name it)."""
    return f"/{NAMESPACE}:{name.lower()}" if valid_name(name) else ""


def bare_name(name: str) -> str:
    text = str(name or "").strip().strip('"')
    if text.lower().startswith(NAMESPACE + ":"):
        text = text.split(":", 1)[1]
    return text.lstrip("/")


def parse_folders(value) -> list[str]:
    """``skill_folders`` setting: a list, or a string with one path per line (``;`` also splits)."""
    if not value:
        return []
    items: Iterable = value if isinstance(value, (list, tuple)) else re.split(r"[\n;]", str(value))
    out: list[str] = []
    for item in items:
        path = str(item or "").strip().strip('"')
        if path and not path.startswith("#") and path not in out:
            out.append(path)
    return out


def invoked_skills(text: str) -> list[str]:
    """Skill names Hermes expanded into this message (single or stacked invocation)."""
    if not text:
        return []
    names: list[str] = []
    for match in _SINGLE.finditer(text):
        names.append(bare_name(match.group(1)))
    for match in _STACKED.finditer(text):
        names.extend(bare_name(part) for part in match.group(1).split(","))
    seen: set[str] = set()
    return [name for name in names if name and not (name.casefold() in seen or seen.add(name.casefold()))]


def leading_tokens(text: str, known) -> tuple[list[str], str]:
    """Leading ``/name`` or ``/triage:name`` tokens that ``known(name)`` accepts.

    Stops at the first word that is not such a token. Returns (names, rest)."""
    names: list[str] = []
    rest = (text or "").lstrip()
    while rest.startswith("/") and len(names) < 5:
        token, _, tail = rest.partition(" ")
        match = _TOKEN.match(token.strip())
        if not match:
            break
        resolved = known(match.group(1))
        if not resolved:
            break
        if resolved.casefold() not in {n.casefold() for n in names}:
            names.append(resolved)
        rest = tail.lstrip()
    return names, rest


def hermes_skill_roots() -> list[Path]:
    """Folders whose skills Hermes already lists in its own ``/`` menu."""
    from .paths import hermes_home

    roots = [hermes_home() / "skills"]
    try:
        from hermes_cli.config import load_config_readonly

        extra = ((load_config_readonly() or {}).get("skills") or {}).get("external_dirs") or []
        roots.extend(Path(str(item)).expanduser() for item in extra if item)
    except Exception:
        pass
    return roots


def _inside_any(path: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except (OSError, ValueError):
            continue
    return False


def register_catalog_skills(ctx, skills, *, native_roots: list[Path] | None = None) -> int:
    """Register catalog skills with Hermes as ``triage:<name>``. Returns the count.

    Skills that live in Hermes' own skill folders are skipped: Hermes already
    offers them as ``/<name>``, and registering them again would list every
    one twice. Triage still indexes them for chips, search and loading."""
    register = getattr(ctx, "register_skill", None)
    if register is None:
        return 0
    roots = hermes_skill_roots() if native_roots is None else native_roots
    count = 0
    for skill in skills:
        if not valid_name(skill.name):
            continue
        source = Path(skill.source) if skill.source else None
        if source is not None and _inside_any(source, roots):
            continue
        path = source / "SKILL.md" if source is not None else None
        if path is None or not path.is_file():
            continue
        try:
            register(skill.name, path, description=skill.description or "")
            count += 1
        except Exception as exc:  # collision, invalid name, or an older host
            logger.debug("triage: skill %s not registered: %s", skill.name, exc)
    return count


def refresh_hermes_skill_commands() -> None:
    """Ask Hermes to rebuild its plugin-skill slash map (best effort, host-private)."""
    try:
        from agent.skill_commands import invalidate_plugin_skill_commands

        invalidate_plugin_skill_commands()
    except Exception:
        pass


def sync_folders(store, folders: list[str]) -> list[str]:
    """Import every configured folder into the catalog. Returns one summary line per folder."""
    from .importer import import_tree

    lines: list[str] = []
    for folder in folders:
        root = Path(folder).expanduser()
        if not root.is_dir():
            lines.append(f"{folder}: not a folder, skipped")
            continue
        try:
            report = import_tree(store, root)
            lines.append(f"{folder}: {report.summary()}")
        except Exception as exc:
            logger.exception("triage: skill folder import failed for %s", folder)
            lines.append(f"{folder}: failed ({exc})")
    return lines


def start_background_sync(ctx, db_path: Path, folders: list[str], before: set[str]) -> threading.Thread | None:
    """Import skill folders off the startup path, then register skills that are new."""
    if not folders:
        return None

    def run() -> None:
        from .store import SkillStore

        # Hermes loads plugins in several processes (gateway, plugin host, web
        # server). Only one of them may import into the shared catalog.
        lock = _acquire_sync_lock(Path(db_path).parent / ".sync.lock")
        if lock is None:
            logger.info("triage: skill folder sync already running in another process")
            return
        store = SkillStore(db_path)
        try:
            for line in sync_folders(store, folders):
                logger.info("triage: %s", line)
            fresh = [skill for skill in store.list_skills() if skill.name.casefold() not in before]
            if fresh and register_catalog_skills(ctx, fresh):
                refresh_hermes_skill_commands()
        except Exception:
            logger.exception("triage: background skill sync failed")
        finally:
            store.close()
            _release_sync_lock(lock)

    thread = threading.Thread(target=run, name="triage-skill-sync", daemon=True)
    thread.start()
    return thread


_STALE_LOCK_SECONDS = 15 * 60


def _acquire_sync_lock(path: Path) -> Path | None:
    import os
    import time

    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > _STALE_LOCK_SECONDS:
                    path.unlink()
                    continue
            except OSError:
                pass
            return None
        except OSError:
            return None
        with os.fdopen(fd, "w") as handle:
            handle.write(str(os.getpid()))
        return path
    return None


def _release_sync_lock(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass
