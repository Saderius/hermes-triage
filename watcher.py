"""Keep the catalog in step with the skill folders, live.

Every connected skill folder (plugin setting ``skill_folders``) is scanned on
gateway start. After that Windows tells Triage the moment anything under a
folder changes (``ReadDirectoryChangesW``); once changes stop for
``SETTLE_SECONDS`` the touched skills are indexed. A slow full check every
``SAFETY_SECONDS`` catches anything a notification missed (other systems use
that check alone, every ``WATCH_SECONDS``). A skill folder is any folder that
holds a ``SKILL.md``; each gets a cheap fingerprint (stat of ``SKILL.md``, the
folder and its direct subfolders), so a check that finds nothing changed reads
no file contents.

* new or changed skill folder -> indexed in place (nothing is copied);
* skill folder gone           -> its skill leaves the catalog, unless another
                                  connected folder still provides that name.

What was imported from where lives in the ``folder_skills`` table, so a skill
deleted while Hermes was closed is also dropped on the next start. Skills that
came into the catalog any other way (``migrate``, ``import``) are never touched.

Hermes runs the plugin in several processes; one of them owns the watcher at a
time through ``.watch.lock`` (refreshed every 30 s, taken over when stale).
A change notification names the file, so an edit deep inside ``references/``
re-indexes its skill too.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger("triage")

WATCH_SECONDS = 30.0
SETTLE_SECONDS = 1.5
SAFETY_SECONDS = 600.0
_LOCK_STALE = 3 * WATCH_SECONDS
_SKIP_DIRS = ("bak-skill-judge", "bak-triage")


def scan_skill_dirs(root: Path) -> dict[str, str]:
    """{skill folder path: fingerprint} for every folder under ``root`` holding a SKILL.md."""
    found: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            name for name in dirnames
            if not name.startswith(".") and not any(skip in name for skip in _SKIP_DIRS)
        ]
        if "SKILL.md" not in filenames:
            continue
        found[dirpath] = _fingerprint(Path(dirpath), dirnames)
    return found


def _fingerprint(folder: Path, subdirs: list[str]) -> str:
    parts: list[str] = []
    for path in [folder / "SKILL.md", folder, *(folder / name for name in sorted(subdirs))]:
        try:
            info = path.stat()
        except OSError:
            continue
        parts.append(f"{path.name}:{info.st_mtime_ns}:{info.st_size}")
    return "|".join(parts)


def reconcile_folder(
    store, root: str, force: set[str] | None = None, earlier: list[str] | tuple = ()
) -> dict[str, list[str]]:
    """Bring the catalog in line with one skill folder. Returns what changed.

    ``earlier`` are the folders listed before this one in the settings: when two
    folders provide the same skill name, the one listed first wins; the other
    copy is recorded (so its deletion is noticed) but not indexed."""
    from .importer import import_tree

    out: dict[str, list[str]] = {"added": [], "updated": [], "removed": [], "failed": []}
    base = Path(root).expanduser()
    known = store.folder_manifest(str(base))  # {dir: (name, fingerprint)}
    if not base.is_dir():
        return out  # an unplugged drive must not wipe the catalog
    current = scan_skill_dirs(base)
    for folder, fingerprint in current.items():
        previous = known.get(folder)
        if previous is not None and previous[1] == fingerprint and folder not in (force or ()):
            continue
        name_here = _skill_name(Path(folder))
        if name_here and _owned_by_earlier(store, name_here, earlier):
            store.record_folder_skill(str(base), folder, name_here, fingerprint)
            continue
        try:
            report = import_tree(store, Path(folder), base=base, recursive=False)
        except Exception as exc:  # one broken skill never stops the rest
            out["failed"].append(f"{folder}: {exc}")
            continue
        names = report.inserted + report.updated + report.unchanged
        if not names:
            out["failed"].extend(report.skipped or [f"{folder}: no skill"])
            continue
        name = names[0]
        if previous is not None and previous[0].casefold() != name.casefold():
            _drop(store, str(base), folder, previous[0], out)
        store.record_folder_skill(str(base), folder, name, fingerprint)
        # A folder's dates move without its skill changing (a file opened and
        # saved unchanged, a tool's temp file). The re-read then finds the same
        # text: that is no change, so it is not counted or logged.
        if report.inserted:
            out["added"].append(name)
        elif report.updated:
            out["updated"].append(name)
    for folder, (name, _fingerprint_old) in known.items():
        if folder not in current:
            _drop(store, str(base), folder, name, out)
    store.point_sources_at_folders(str(base), tuple(_norm_roots(earlier)))
    return out


def _skill_name(folder: Path) -> str:
    from .frontmatter import split_frontmatter

    try:
        meta, _body = split_frontmatter((folder / "SKILL.md").read_text(encoding="utf-8"))
    except OSError:
        return ""
    return str(meta.get("name") or folder.name).strip()


def _norm_roots(roots) -> list[str]:
    return [str(Path(root).expanduser()) for root in roots or ()]


def _owned_by_earlier(store, name: str, earlier) -> bool:
    wanted = {os.path.normcase(root) for root in _norm_roots(earlier)}
    return any(
        os.path.normcase(root) in wanted and (Path(folder) / "SKILL.md").is_file()
        for root, folder in store.folder_skill_places(name)
    )


def _heal(store, folders: list[str]) -> int:
    """Skills whose winning folder vanished are re-read from the next folder
    (in settings order) that still provides them. Returns how many."""
    from .importer import import_tree

    order = {os.path.normcase(root): index for index, root in enumerate(_norm_roots(folders))}
    healed = 0
    for skill in store.list_skills():
        source = Path(skill.source) if skill.source else None
        if source is not None and (source / "SKILL.md").is_file():
            continue
        places = sorted(
            (order.get(os.path.normcase(root), 999), root, folder)
            for root, folder in store.folder_skill_places(skill.name)
            if (Path(folder) / "SKILL.md").is_file()
        )
        if places:
            _rank, root, folder = places[0]
            import_tree(store, Path(folder), base=Path(root), recursive=False)
            healed += 1
    return healed


def reconcile_touched(store, root: str, paths: set[str], earlier: list[str] | tuple = ()) -> dict[str, list[str]]:
    """Index only what a change notification touched under ``root`` (no full walk).

    A touched path inside a skill folder re-indexes that skill; a recorded
    skill folder that no longer has its SKILL.md leaves the catalog."""
    from .importer import import_tree

    out: dict[str, list[str]] = {"added": [], "updated": [], "removed": [], "failed": []}
    base = Path(root).expanduser()
    if not base.is_dir():
        return out
    base_key = os.path.normcase(str(base))
    mine = {p for p in paths if os.path.normcase(p).startswith(base_key)}
    if not mine:
        return out
    known = store.folder_manifest(str(base))
    for folder, (name, _old) in known.items():
        key = os.path.normcase(folder)
        if any(os.path.normcase(p).startswith(key) or key.startswith(os.path.normcase(p)) for p in mine):
            if not (Path(folder) / "SKILL.md").is_file():
                _drop(store, str(base), folder, name, out)
    for folder in sorted(_skill_dirs_of(mine)):
        if not os.path.normcase(folder).startswith(base_key):
            continue
        subdirs = [entry.name for entry in os.scandir(folder) if entry.is_dir() and not entry.name.startswith(".")]
        fingerprint = _fingerprint(Path(folder), subdirs)
        previous = known.get(folder)
        name_here = _skill_name(Path(folder))
        if name_here and _owned_by_earlier(store, name_here, earlier):
            store.record_folder_skill(str(base), folder, name_here, fingerprint)
            continue
        try:
            report = import_tree(store, Path(folder), base=base, recursive=False)
        except Exception as exc:
            out["failed"].append(f"{folder}: {exc}")
            continue
        names = report.inserted + report.updated + report.unchanged
        if not names:
            continue
        if previous is not None and previous[0].casefold() != names[0].casefold():
            _drop(store, str(base), folder, previous[0], out)
        store.record_folder_skill(str(base), folder, names[0], fingerprint)
        if report.inserted:
            out["added"].append(names[0])
        elif report.updated:
            out["updated"].append(names[0])
    return out


def _drop(store, root: str, folder: str, name: str, out: dict) -> None:
    store.forget_folder_skill(root, folder)
    if store.folder_skill_owners(name):
        return  # another connected folder still provides this name
    if store.delete(name):
        out["removed"].append(name)


def reconcile_all(store, folders: list[str], force: set[str] | None = None) -> list[str]:
    """One pass over every folder; returns log lines for folders that changed.

    ``force`` holds skill folders a change notification touched: they are
    re-indexed even when their fingerprint looks the same."""
    lines: list[str] = []
    for index, folder in enumerate(folders):
        change = reconcile_folder(store, folder, force, earlier=folders[:index])
        if any(change.values()):
            lines.append(
                f"{folder}: +{len(change['added'])} new, ~{len(change['updated'])} changed, "
                f"-{len(change['removed'])} removed"
                + (f", {len(change['failed'])} failed" if change["failed"] else "")
            )
    # A folder taken out of the settings takes its skills out of the catalog
    # (unless another connected folder still provides the same name).
    connected = {os.path.normcase(str(Path(folder).expanduser())) for folder in folders}
    for old_root in store.folder_roots():
        if os.path.normcase(old_root) in connected:
            continue
        out: dict[str, list[str]] = {"removed": []}
        for folder, (name, _old) in store.folder_manifest(old_root).items():
            _drop(store, old_root, folder, name, out)
        lines.append(f"{old_root}: disconnected, -{len(out['removed'])} removed")
    healed = _heal(store, folders)
    if healed:
        lines.append(f"{healed} skill(s) now read from their next folder")
    return lines


def start_folder_watcher(ctx, db_path: Path, folders, interval: float = WATCH_SECONDS, after_index=None):
    """Daemon thread that keeps the catalog in step with the skill folders.

    One process at a time owns it (``.watch.lock``). The owner indexes on
    start, then on change notifications (settled for ``SETTLE_SECONDS``), with a
    full check every ``SAFETY_SECONDS``; where notifications are unavailable it
    checks every ``interval`` seconds. ``folders`` is a list or a callable, so
    folders connected in the settings later are picked up without a restart."""
    current = folders if callable(folders) else (lambda: list(folders))
    lock_path = Path(db_path).parent / ".watch.lock"
    changes = _Changes()

    def index(wanted: list[str], touched: set[str], full: bool) -> None:
        from .catalog_link import refresh_hermes_skill_commands, register_catalog_skills
        from .store import SkillStore

        store = SkillStore(db_path)
        try:
            before = {skill.name.casefold() for skill in store.list_skills()}
            if touched and not full:
                # A notification: index just the skill folders it named.
                for index, root in enumerate(wanted):
                    change = reconcile_touched(store, root, touched, earlier=wanted[:index])
                    if any(change.values()):
                        logger.info(
                            "triage: %s: +%d new, ~%d changed, -%d removed (live)",
                            root, len(change["added"]), len(change["updated"]), len(change["removed"]),
                        )
                    if change["removed"] or change["added"]:
                        _heal(store, wanted)
            else:
                for line in reconcile_all(store, wanted, _skill_dirs_of(touched)):
                    logger.info("triage: %s", line)
            fresh = [skill for skill in store.list_skills() if skill.name.casefold() not in before]
            if ctx is not None and fresh and register_catalog_skills(ctx, fresh):
                refresh_hermes_skill_commands()
            if after_index is not None:
                after_index(store)
        except Exception:
            logger.exception("triage: skill folder watch failed")
        finally:
            store.close()

    def run() -> None:
        watching: dict[str, bool] = {}
        last_full = 0.0
        last_wanted: set[str] = set()
        last_hook = 0.0
        while True:
            try:
                wanted = [str(Path(folder).expanduser()) for folder in (current() or [])]
            except Exception:
                logger.exception("triage: reading skill_folders failed")
                wanted = []
            if wanted and _hold_lock(lock_path):
                for root in wanted:
                    if root not in watching and Path(root).is_dir():
                        watching[root] = _start_native_watch(root, changes)
                live = all(watching.get(root) for root in wanted)
                # A folder connected or disconnected in the settings gets a full
                # pass at once (notifications only cover changes from now on).
                changed_roots = set(wanted) != last_wanted
                last_wanted = set(wanted)
                due = changed_roots or time.time() - last_full >= (SAFETY_SECONDS if live else interval)
                touched = changes.take(SETTLE_SECONDS)
                if due or touched:
                    index(wanted, touched, due)
                    if due:
                        last_full = time.time()
                    last_hook = time.time()
                elif after_index is not None and time.time() - last_hook >= 15.0:
                    # Settings such as "Hide Hermes' own skills" can change
                    # without any folder changing.
                    last_hook = time.time()
                    from .store import SkillStore

                    store = SkillStore(db_path)
                    try:
                        after_index(store)
                    except Exception:
                        logger.exception("triage: after-index hook failed")
                    finally:
                        store.close()
                # Short wait: a folder (dis)connected in the settings shows up in ~3 s.
                changes.wait(min(interval, 3.0))
            else:
                time.sleep(interval)

    thread = threading.Thread(target=run, name="triage-folder-watch", daemon=True)
    thread.start()
    return thread


class _Changes:
    """Paths reported by change notifications, handed out once they settle."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._paths: set[str] = set()
        self._last = 0.0

    def add(self, path: str) -> None:
        with self._lock:
            self._paths.add(path)
            self._last = time.time()
        self._event.set()

    def wait(self, timeout: float) -> None:
        self._event.wait(timeout)

    def take(self, settle: float) -> set[str]:
        """Changed paths, after no new change for ``settle`` seconds (blocks until then)."""
        while True:
            with self._lock:
                if not self._paths:
                    self._event.clear()
                    return set()
                quiet = time.time() - self._last
                if quiet >= settle:
                    paths, self._paths = self._paths, set()
                    self._event.clear()
                    return paths
            time.sleep(max(0.05, settle - quiet))


def _skill_dirs_of(paths: set[str]) -> set[str]:
    """The skill folder (nearest parent holding a SKILL.md) of each changed path."""
    found: set[str] = set()
    for raw in paths:
        path = Path(raw)
        for candidate in [path, *path.parents][:12]:
            if (candidate / "SKILL.md").is_file():
                found.add(str(candidate))
                break
    return found


def _start_native_watch(root: str, changes: _Changes) -> bool:
    """Watch ``root`` recursively with ReadDirectoryChangesW. False where unavailable."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel32.CreateFileW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                           wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        read = kernel32.ReadDirectoryChangesW
        read.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
                         ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, ctypes.c_void_p]
        read.restype = wintypes.BOOL
        handle = create(root, 0x0001, 0x7, None, 3, 0x02000000, None)  # list dir, share all, open existing, backup semantics
        if not handle or handle == ctypes.c_void_p(-1).value:
            return False
    except Exception:
        logger.exception("triage: change notifications unavailable for %s", root)
        return False

    def loop() -> None:
        buffer = ctypes.create_string_buffer(64 * 1024)
        returned = wintypes.DWORD()
        flags = 0x1 | 0x2 | 0x8 | 0x10  # file name, dir name, size, last write
        while True:
            if not read(handle, buffer, len(buffer), True, flags, ctypes.byref(returned), None, None):
                logger.warning("triage: change notifications stopped for %s", root)
                return
            if returned.value == 0:  # overflow: too many changes at once
                changes.add(root)
                continue
            offset = 0
            while True:
                next_offset, _action, length = (
                    int.from_bytes(buffer.raw[offset + start:offset + start + 4], "little") for start in (0, 4, 8)
                )
                name = buffer.raw[offset + 12:offset + 12 + length].decode("utf-16-le", "replace")
                if not any(part.startswith(".") for part in Path(name).parts):  # .git and friends
                    changes.add(os.path.join(root, name))
                if not next_offset:
                    break
                offset += next_offset

    threading.Thread(target=loop, name=f"triage-watch-{Path(root).name}", daemon=True).start()
    return True


def _alive(pid_text: str) -> bool:
    """True unless the process that owns the lock is known to be gone (so a
    restarted gateway takes over at once instead of waiting for staleness)."""
    try:
        pid = int(pid_text)
    except ValueError:
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                return code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _hold_lock(path: Path) -> bool:
    """Take or keep the watcher lock for this process (pid inside, mtime refreshed)."""
    pid = str(os.getpid())
    try:
        owner = path.read_text(encoding="utf-8").strip()
        age = time.time() - path.stat().st_mtime
    except OSError:
        owner, age = "", float("inf")
    if owner and owner != pid and age < _LOCK_STALE and _alive(owner):
        return False
    try:
        path.write_text(pid, encoding="utf-8")
        os.utime(path, None)
    except OSError:
        return False
    return path.read_text(encoding="utf-8").strip() == pid


def discover_skill_roots() -> list[tuple[str, int]]:
    """Folders on this PC that commonly hold SKILL.md skills, with a count each.

    Only suggestions for the ``skill_folders`` setting; nothing is connected
    automatically."""
    home = Path.home()
    candidates = [
        home / ".claude" / "skills",
        home / ".agents" / "skills",
        home / ".codex" / "skills",
        home / ".gemini" / "skills",
        home / ".gemini" / "antigravity-cli" / "skills",
        home / ".cursor" / "skills",
    ]
    for pattern_root in (home / ".claude" / "plugins" / "cache",):
        if pattern_root.is_dir():
            candidates.extend(sorted(pattern_root.glob("*/*/*/skills")))
    out: list[tuple[str, int]] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path).casefold()
        if key in seen or not path.is_dir():
            continue
        seen.add(key)
        count = len(scan_skill_dirs(path))
        if count:
            out.append((str(path), count))
    return out
