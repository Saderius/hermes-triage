"""Tests for favourites, most used, and compaction-sticky skills."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

import support
from triage.importer import import_tree
from triage.store import SkillStore


class FavouritesStickyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="hsj-fav-sticky-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.store = SkillStore(self.tmp / "catalog.db")
        self.addCleanup(self.store.close)
        import_tree(self.store, support.DEMO_SKILLS)

    def test_favorites(self) -> None:
        self.assertEqual(self.store.favorites(), [])
        self.store.set_favorite("git-commit", True)
        time.sleep(0.01)
        self.store.set_favorite("PR-Review", True)
        self.assertEqual(self.store.favorites(), ["git-commit", "pr-review"])
        self.store.set_favorite("git-commit", True)
        self.assertEqual(self.store.favorites(), ["git-commit", "pr-review"])
        self.store.set_favorite("GIT-COMMIT", False)
        self.assertEqual(self.store.favorites(), ["pr-review"])

    def test_favorites_unindexed(self) -> None:
        self.store.set_favorite("no-such-skill", True)
        self.assertEqual(self.store.favorites(), [])

    def test_most_used(self) -> None:
        self.assertEqual(self.store.most_used(), [])
        self.store.bump_use("weather-brief")
        self.store.bump_use("weather-brief")
        self.store.bump_use("git-commit")
        self.assertEqual(self.store.most_used(), ["weather-brief", "git-commit"])
        self.assertEqual(self.store.most_used(1), ["weather-brief"])

    def test_sticky(self) -> None:
        self.assertEqual(self.store.sticky([]), [])
        self.store.set_sticky("a", "git-commit", True)
        self.store.set_sticky("b", "pr-review", True)
        self.assertEqual(self.store.sticky(["a"]), ["git-commit"])
        self.assertEqual(self.store.sticky(["a", "b"]), ["git-commit", "pr-review"])
        self.assertEqual(self.store.sticky(["c"]), [])
        self.store.set_sticky("a", "Git-Commit", False)
        self.assertEqual(self.store.sticky(["a", "b"]), ["pr-review"])

    def test_reopen_survives(self) -> None:
        self.store.set_favorite("pr-review", True)
        self.store.set_sticky("session-1", "git-commit", True)
        db_path = self.store.db_path
        self.store.close()

        reopened = SkillStore(db_path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.favorites(), ["pr-review"])
        self.assertEqual(reopened.sticky(["session-1"]), ["git-commit"])


if __name__ == "__main__":
    unittest.main()
