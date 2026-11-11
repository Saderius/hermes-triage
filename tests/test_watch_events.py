"""Change notifications: a skill appears, changes and disappears within seconds."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import support  # noqa: F401
from triage.store import SkillStore
from triage.watcher import start_folder_watcher


def _until(check, timeout: float = 8.0) -> float | None:
    start = time.time()
    while time.time() - start < timeout:
        if check():
            return time.time() - start
        time.sleep(0.1)
    return None


@unittest.skipUnless(os.name == "nt", "change notifications are Windows-only; elsewhere the timer is used")
class WatchEventTests(unittest.TestCase):
    def test_add_edit_reference_and_delete_are_picked_up_quickly(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="triage-events-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        root = tmp / "skills"
        root.mkdir()
        db = tmp / "data" / "catalog.db"
        SkillStore(db).close()
        start_folder_watcher(None, db, [str(root)], interval=30.0)
        time.sleep(1.0)  # first full pass

        def get(name):
            store = SkillStore(db)
            try:
                return store.get(name)
            finally:
                store.close()

        folder = root / "fresh-skill"
        (folder / "references").mkdir(parents=True)
        (folder / "SKILL.md").write_text("---\nname: fresh-skill\ndescription: New.\n---\n\nBODY\n", encoding="utf-8")
        added = _until(lambda: get("fresh-skill") is not None)
        self.assertIsNotNone(added, "new skill not indexed")
        self.assertLess(added, 4.0)

        (folder / "references" / "deep.md").write_text("DEEP_REFERENCE_MARKER\n", encoding="utf-8")
        edited = _until(lambda: "DEEP_REFERENCE_MARKER" in (get("fresh-skill").refs or ""))
        self.assertIsNotNone(edited, "edit deep in references/ not re-indexed")

        shutil.rmtree(folder)
        gone = _until(lambda: get("fresh-skill") is None)
        self.assertIsNotNone(gone, "deleted skill still in the catalog")



@unittest.skipUnless(os.name == "nt", "change notifications are Windows-only")
class ConnectFolderTests(unittest.TestCase):
    def test_folder_connected_later_is_indexed_at_once(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="triage-connect-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        first, later = tmp / "first", tmp / "later"
        first.mkdir()
        existing = later / "already-there"
        existing.mkdir(parents=True)
        (existing / "SKILL.md").write_text("---\nname: already-there\ndescription: x\n---\n\nB\n", encoding="utf-8")
        db = tmp / "data" / "catalog.db"
        SkillStore(db).close()
        folders = [str(first)]
        start_folder_watcher(None, db, lambda: list(folders), interval=1.0)
        time.sleep(1.5)
        folders.append(str(later))  # connected in the settings while running

        def present() -> bool:
            store = SkillStore(db)
            try:
                return store.get("already-there") is not None
            finally:
                store.close()

        self.assertIsNotNone(_until(present, timeout=8.0), "skills already in a newly connected folder were not indexed")


if __name__ == "__main__":
    unittest.main()
