"""Session bridges survive compaction; the follow-up stub is names and descriptions only."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import support
from triage.importer import import_tree
from triage.service import Runtime, Settings
from triage.store import SkillStore


class BridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="hsj-bridge-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.store = SkillStore(self.tmp / "catalog.db")
        self.addCleanup(self.store.close)
        import_tree(self.store, support.DEMO_SKILLS)
        self.store.upsert(
            name="android-debug",
            description="Debug Android Kotlin apps with Logcat.",
            body="ANDROID_ONLY_BODY " + ("logcat " * 80),
            tags=["android"],
            source="",
            origin_path="android-debug/SKILL.md",
            content_hash="android",
            refs="",
        )
        self.runtime = Runtime(self.store, settings=Settings())

    def test_post_compaction_keeps_allowlist_and_lists_names_only(self) -> None:
        self.assertEqual(self.runtime.settings().auto_accept_threshold, 0.90)
        self.runtime.ledger.approve("turn:before", ["android-debug", "git-commit"], "sess")
        self.runtime.memory.remember("sess", self.runtime.judge.rank("review the commit message"))
        self.runtime.on_post_compaction(
            session_id="sess",
            old_session_id="",
            kind="summarize",
            reason="manual",
        )
        self.assertIsNone(self.runtime.memory.shortlist("sess"))
        self.assertTrue(self.store.session_compacted("sess"))
        self.assertIsNone(self.runtime.on_pre_tool(
            tool_name="skill_view",
            args={"name": "android-debug"},
            session_id="sess",
            turn_id="after",
        ))
        self.assertIsNone(self.runtime.on_pre_tool(
            tool_name="skill_view",
            args={"name": "git-commit", "file_path": "notes.md"},
            session_id="sess",
            turn_id="after",
        ))
        blocked = self.runtime.on_pre_tool(
            tool_name="skill_view",
            args={"name": "weather-brief"},
            session_id="sess",
            turn_id="after",
        )
        self.assertEqual(blocked["action"], "block")

        decision = self.runtime.on_pre_llm(
            user_message="thanks, continue",
            session_id="sess",
            turn_id="after",
            platform="cli",
        )
        stub = decision["context"]
        self.assertIn("Skills unlocked in this chat", stub)
        self.assertIn("android-debug:", stub)
        self.assertIn("git-commit:", stub)
        self.assertIn("Logcat", stub)
        self.assertNotIn("ANDROID_ONLY_BODY", stub)
        self.assertNotIn("COMMIT_BODY_ONLY", stub)
        self.assertNotIn("<available_skills>", stub)
        self.assertNotIn("weather-brief:", stub)

        shortlist = self.runtime.on_pre_llm(
            user_message="review the commit message",
            session_id="sess",
            turn_id="suggest",
            platform="cli",
        )
        fresh = shortlist["context"]
        self.assertIn("android-debug", fresh)
        self.assertIn("skill_view is allowed", fresh)
        self.assertNotIn("Nothing below is loaded yet", fresh)
        self.assertNotIn("Do not call skill_view until one of them is approved", fresh)
        self.assertNotIn("ANDROID_ONLY_BODY", fresh)

        again = self.runtime.on_pre_llm(
            user_message="thanks, continue",
            session_id="sess",
            turn_id="later",
            platform="cli",
        )
        if again and again.get("context"):
            self.assertNotIn("Skills unlocked in this chat", again["context"])

    def test_compaction_rotates_the_session_bridge(self) -> None:
        self.runtime.ledger.approve("turn:old", ["git-commit"], "old-sess")
        self.runtime.on_post_compaction(
            session_id="new-sess",
            old_session_id="old-sess",
            kind="summarize",
            reason="overflow",
        )
        self.assertIsNone(self.runtime.on_pre_tool(
            tool_name="skill_view",
            args={"name": "git-commit"},
            session_id="new-sess",
            turn_id="rotated",
        ))
        stub = self.runtime.unlocked_stub("new-sess")
        self.assertIn("git-commit:", stub)
        self.assertNotIn("COMMIT_BODY_ONLY", stub)


if __name__ == "__main__":
    unittest.main()