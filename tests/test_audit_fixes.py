"""Tests for the 2026-11-10 backend audit fixes."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import support
from triage.catalog_io import mirror_dir
from triage.importer import _valid_name, import_tree
from triage.service import Runtime, Settings
from triage.store import SkillStore


class AuditFixesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="hsj-audit-fixes-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.store = SkillStore(self.tmp / "catalog.db")
        self.addCleanup(self.store.close)
        import_tree(self.store, support.DEMO_SKILLS)
        self.runtime = Runtime(self.store, settings=Settings())

    def test_valid_name(self) -> None:
        self.assertFalse(_valid_name(".."))
        self.assertFalse(_valid_name("."))
        self.assertFalse(_valid_name(" . "))
        self.assertTrue(_valid_name("git-commit"))

    def test_mirror_dir_unsafe(self) -> None:
        with self.assertRaises(ValueError):
            mirror_dir(self.store, "..")

    def test_first_turn_fresh_pile_platform(self) -> None:
        self.store.set_pending_attach("runtime-id", ["git-commit"], fresh=True)
        self.runtime.on_pre_llm(
            user_message="hello",
            session_id="tg-session",
            turn_id="t-tg",
            platform="telegram",
            is_first_turn=True,
            conversation_history=[],
        )
        self.assertEqual(self.store.get_pending_attach("runtime-id"), ["git-commit"])
        result = self.runtime.on_pre_llm(
            user_message="commit this",
            session_id="local-session",
            turn_id="t-local",
            platform="local",
            is_first_turn=True,
            conversation_history=[],
        )
        self.assertIn("COMMIT_BODY_ONLY", (result or {}).get("context", ""))
        self.assertEqual(self.store.get_pending_attach("runtime-id"), [])

    def test_pending_autos(self) -> None:
        self.store.set_pending_attach("s", ["git-commit", "pr-review"], autos=["PR-Review", "unknown"])
        self.assertEqual(self.store.get_pending_autos("s"), ["pr-review"])
        self.store.set_pending_attach("s", [])
        self.assertEqual(self.store.get_pending_autos("s"), [])

    def test_catalog_signature(self) -> None:
        sig1 = self.store.catalog_signature()
        self.store.upsert(
            name="new-audit-skill",
            description="A skill added after the first signature.",
            body="Body.",
            tags=[],
            source=str(self.tmp),
            origin_path="new-audit-skill/SKILL.md",
            content_hash="x",
            refs="",
        )
        sig2 = self.store.catalog_signature()
        self.assertNotEqual(sig1, sig2)


if __name__ == "__main__":
    unittest.main()
