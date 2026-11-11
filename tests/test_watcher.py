"""Skill folders stay in sync live: new, changed and deleted skills."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import support  # noqa: F401  (sets up the triage package)
from triage.store import SkillStore
from triage.watcher import reconcile_folder, scan_skill_dirs


def write_skill(root: Path, name: str, body: str = "Body text.") -> Path:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} does things\n---\n\n{body}\n", encoding="utf-8"
    )
    return folder


def bump(path: Path) -> None:
    later = time.time() + 5
    os.utime(path, (later, later))


class WatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="triage-watch-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.store = SkillStore(self.tmp / "data" / "catalog.db")
        self.addCleanup(self.store.close)
        self.root = self.tmp / "skills"
        self.root.mkdir()

    def test_new_changed_and_deleted_skills_follow_the_folder(self) -> None:
        write_skill(self.root, "alpha")
        beta = write_skill(self.root, "beta")
        first = reconcile_folder(self.store, str(self.root))
        self.assertEqual(sorted(first["added"]), ["alpha", "beta"])
        self.assertIsNotNone(self.store.get("beta"))

        quiet = reconcile_folder(self.store, str(self.root))
        self.assertFalse(any(quiet.values()), quiet)

        (beta / "SKILL.md").write_text("---\nname: beta\ndescription: beta v2\n---\n\nNEW_BODY\n", encoding="utf-8")
        bump(beta / "SKILL.md")
        changed = reconcile_folder(self.store, str(self.root))
        self.assertEqual(changed["updated"], ["beta"])
        self.assertIn("NEW_BODY", self.store.get("beta").body)

        shutil.rmtree(beta)
        gone = reconcile_folder(self.store, str(self.root))
        self.assertEqual(gone["removed"], ["beta"])
        self.assertIsNone(self.store.get("beta"))
        self.assertIsNotNone(self.store.get("alpha"))

    def test_a_moved_date_with_the_same_text_is_no_change(self) -> None:
        alpha = write_skill(self.root, "alpha")
        reconcile_folder(self.store, str(self.root))
        # Something touched the folder (a temp file came and went, a file was
        # saved unchanged): its dates move, the skill's text does not.
        (alpha / "tmp.txt").write_text("x", encoding="utf-8")
        (alpha / "tmp.txt").unlink()
        bump(alpha)
        bump(alpha / "SKILL.md")
        again = reconcile_folder(self.store, str(self.root))
        self.assertFalse(any(again.values()), again)
        # The new dates are recorded, so the next check reads nothing.
        self.assertFalse(any(reconcile_folder(self.store, str(self.root)).values()))

    def test_skills_from_elsewhere_and_shared_names_survive(self) -> None:
        self.store.upsert(name="manual", description="imported by migrate", body="x", tags=[], source="",
                          origin_path="", content_hash="h", refs="")
        other = self.tmp / "other"
        write_skill(self.root, "shared")
        write_skill(other, "shared")
        reconcile_folder(self.store, str(self.root))
        reconcile_folder(self.store, str(other))
        shutil.rmtree(self.root / "shared")
        result = reconcile_folder(self.store, str(self.root))
        self.assertEqual(result["removed"], [])
        self.assertIsNotNone(self.store.get("shared"))
        self.assertIsNotNone(self.store.get("manual"))

    def test_missing_root_never_wipes_the_catalog(self) -> None:
        write_skill(self.root, "alpha")
        reconcile_folder(self.store, str(self.root))
        shutil.rmtree(self.root)
        result = reconcile_folder(self.store, str(self.root))
        self.assertFalse(any(result.values()))
        self.assertIsNotNone(self.store.get("alpha"))

    def test_scan_finds_nested_skill_folders(self) -> None:
        write_skill(self.root / "group", "nested")
        write_skill(self.root, ".hidden")
        found = scan_skill_dirs(self.root)
        self.assertEqual([Path(path).name for path in found], ["nested"])


class BridgeTests(unittest.TestCase):
    def test_folder_skills_are_indexed_in_place_not_copied(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="triage-bridge-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        store = SkillStore(tmp / "data" / "catalog.db")
        self.addCleanup(store.close)
        folder = write_skill(tmp / "skills", "alpha")
        reconcile_folder(store, str(tmp / "skills"))
        self.assertEqual(Path(store.get("alpha").source).resolve(), folder.resolve())
        self.assertFalse((tmp / "data" / "mirrors").exists())



class LockTests(unittest.TestCase):
    def test_lock_of_a_dead_process_is_taken_over_at_once(self) -> None:
        from triage.watcher import _alive, _hold_lock

        tmp = Path(tempfile.mkdtemp(prefix="triage-lock-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        lock = tmp / ".watch.lock"
        self.assertTrue(_alive(str(os.getpid())))
        lock.write_text("999999", encoding="utf-8")  # no such process, lock looks fresh
        self.assertFalse(_alive("999999"))
        self.assertTrue(_hold_lock(lock))
        self.assertEqual(lock.read_text(encoding="utf-8").strip(), str(os.getpid()))



class DisconnectTests(unittest.TestCase):
    def test_removing_a_folder_from_settings_removes_its_skills(self) -> None:
        from triage.watcher import reconcile_all

        tmp = Path(tempfile.mkdtemp(prefix="triage-disc-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        store = SkillStore(tmp / "data" / "catalog.db")
        self.addCleanup(store.close)
        first, second = tmp / "one", tmp / "two"
        write_skill(first, "alpha")
        write_skill(second, "beta")
        write_skill(second, "alpha")  # same name in both folders
        reconcile_all(store, [str(first), str(second)])
        self.assertIsNotNone(store.get("beta"))
        lines = reconcile_all(store, [str(first)])
        self.assertIsNone(store.get("beta"))
        self.assertIsNotNone(store.get("alpha"))  # still provided by the first folder
        self.assertTrue(any("disconnected" in line for line in lines))



class PrecedenceTests(unittest.TestCase):
    def test_first_listed_folder_wins_a_shared_name_and_the_other_takes_over(self) -> None:
        from triage.watcher import reconcile_all

        tmp = Path(tempfile.mkdtemp(prefix="triage-prec-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        store = SkillStore(tmp / "data" / "catalog.db")
        self.addCleanup(store.close)
        first, second = tmp / "first", tmp / "second"
        write_skill(first, "pdf", body="FIRST_COPY")
        write_skill(second, "pdf", body="SECOND_COPY")
        for _ in range(3):  # repeated passes never flip the winner
            reconcile_all(store, [str(first), str(second)])
            self.assertIn("FIRST_COPY", store.get("pdf").body)
            self.assertEqual(Path(store.get("pdf").source).resolve(), (first / "pdf").resolve())
        shutil.rmtree(first / "pdf")
        reconcile_all(store, [str(first), str(second)])
        self.assertIn("SECOND_COPY", store.get("pdf").body)
        self.assertEqual(Path(store.get("pdf").source).resolve(), (second / "pdf").resolve())


if __name__ == "__main__":
    unittest.main()
