"""Tests for session_family and following chat session replacements."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import support
import triage.context as context_mod
from triage.context import session_family
from triage.importer import import_tree
from triage.service import Runtime, Settings
from triage.store import SkillStore


class SessionFamilyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="hsj-session-family-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.store = SkillStore(self.tmp / "catalog.db")
        self.addCleanup(self.store.close)
        import_tree(self.store, support.DEMO_SKILLS)
        self.runtime = Runtime(self.store, settings=Settings())

        self.state = self.tmp / "state.db"
        conn = sqlite3.connect(self.state)
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT, started_at REAL)")
        conn.executemany(
            "INSERT INTO sessions VALUES (?, ?, ?)",
            [("old", None, 1.0), ("fork", "old", 2.0), ("new", "old", 3.0), ("newer", "new", 4.0)],
        )
        conn.commit()
        conn.close()

    def test_session_family_parents_and_children(self) -> None:
        self.assertEqual(session_family("new", db_path=self.state), ["old", "newer"])

    def test_session_family_newest_child(self) -> None:
        self.assertEqual(session_family("old", db_path=self.state), ["new", "newer"])

    def test_session_family_empty_and_missing(self) -> None:
        self.assertEqual(session_family("", db_path=self.state), [])
        self.assertEqual(session_family("new", db_path=self.tmp / "missing.db"), [])

    def test_pile_follows_chat(self) -> None:
        original = context_mod.state_db_path
        context_mod.state_db_path = lambda: self.state
        self.addCleanup(setattr, context_mod, "state_db_path", original)

        self.store.set_pending_attach("old", ["git-commit"])
        result = self.runtime.on_pre_llm(
            user_message="commit this",
            session_id="new",
            turn_id="t1",
            platform="local",
            is_first_turn=False,
            conversation_history=[],
        )
        self.assertIn("COMMIT_BODY_ONLY", (result or {}).get("context", ""))
        self.assertEqual(self.store.get_pending_attach("old"), [])

    def test_own_pile_wins_and_clears_other(self) -> None:
        original = context_mod.state_db_path
        context_mod.state_db_path = lambda: self.state
        self.addCleanup(setattr, context_mod, "state_db_path", original)

        self.store.set_pending_attach("old", ["pr-review"])
        self.store.set_pending_attach("new", ["git-commit"])
        result = self.runtime.on_pre_llm(
            user_message="commit this",
            session_id="new",
            turn_id="t1",
            platform="local",
            is_first_turn=False,
            conversation_history=[],
        )
        ctx = (result or {}).get("context", "")
        self.assertIn("COMMIT_BODY_ONLY", ctx)
        self.assertNotIn("REVIEW_BODY_ONLY", ctx)
        self.assertEqual(self.store.get_pending_attach("old"), [])


if __name__ == "__main__":
    unittest.main()
