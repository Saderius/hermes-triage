"""Attached skills: /triage:name tokens, Hermes-expanded invocations, registration, folders."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import support
from triage.catalog_link import (
    invoked_skills,
    leading_tokens,
    parse_folders,
    register_catalog_skills,
    slash_token,
    sync_folders,
)
from triage.importer import import_tree
from triage.service import Runtime, Settings
from triage.store import SkillStore


class FakeCtx:
    def __init__(self) -> None:
        self.skills: dict[str, Path] = {}

    def register_skill(self, name, path, description=""):
        if ":" in name:
            raise ValueError("no colons")
        self.skills[name] = Path(path)


class AttachTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="triage-attach-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.store = SkillStore(self.tmp / "catalog.db")
        self.addCleanup(self.store.close)
        import_tree(self.store, support.DEMO_SKILLS)
        self.runtime = Runtime(self.store, settings=Settings())

    def turn(self, text, session="s"):
        return self.runtime.on_pre_llm(
            user_message=text, turn_id=f"t-{text[:10]}", session_id=session, platform="desktop",
            is_first_turn=False, conversation_history=[{"role": "user", "content": "earlier"}],
        )

    def test_slash_token_and_folder_parsing(self) -> None:
        self.assertEqual(slash_token("Git-Commit"), "/triage:git-commit")
        self.assertEqual(slash_token("bad name"), "")
        self.assertEqual(parse_folders("C:/a; C:/b\nC:/a"), ["C:/a", "C:/b"])

    def test_invoked_skills_reads_single_and_stacked_markers(self) -> None:
        single = '[IMPORTANT: The user has invoked the "triage:git-commit" skill, indicating they want ...]'
        self.assertEqual(invoked_skills(single), ["git-commit"])
        stacked = (
            '[IMPORTANT: The user has invoked the "/triage:git-commit /triage:pr-review" stacked skill bundle, '
            "loading 2 skills together.]\n\nSkills loaded: triage:git-commit, triage:pr-review\n"
        )
        self.assertEqual(invoked_skills(stacked), ["git-commit", "pr-review"])

    def test_leading_tokens_stop_at_the_first_word(self) -> None:
        known = lambda name: {"git-commit": "git-commit", "pr-review": "pr-review"}.get(name)
        names, rest = leading_tokens("/triage:git-commit /pr-review fix it /triage:pr-review", known)
        self.assertEqual(names, ["git-commit", "pr-review"])
        self.assertEqual(rest, "fix it /triage:pr-review")
        self.assertEqual(leading_tokens("/unknown do x", known), ([], "/unknown do x"))

    def test_text_tokens_load_bodies_and_skip_the_shortlist(self) -> None:
        result = self.turn("/triage:git-commit /triage:pr-review review the commit message")
        self.assertIn("Skills attached to this message", result["context"])
        self.assertIn("COMMIT_BODY_ONLY", result["context"])
        self.assertNotIn("probability", result["context"].lower())
        self.assertTrue(self.runtime.ledger.allows("x", "pr-review", "s"))

    def test_hermes_expanded_message_adds_nothing_but_records_the_fan(self) -> None:
        text = (
            '[IMPORTANT: The user has invoked the "triage:git-commit" skill, indicating they want you '
            "to follow its instructions. The full skill content is loaded below.]\n\n# git-commit ..."
        )
        result = self.turn(text)
        self.assertIsNone(result)
        rows = self.store.recent_selections(5)
        self.assertTrue(any(row.get("chosen") == "git-commit" and row.get("accepted") for row in rows))

    def test_register_catalog_skills_uses_mirrors(self) -> None:
        ctx = FakeCtx()
        count = register_catalog_skills(ctx, self.store.list_skills())
        self.assertEqual(count, 3)
        self.assertTrue(all(path.name == "SKILL.md" and path.is_file() for path in ctx.skills.values()))

    def test_sync_folders_imports_and_reports_missing(self) -> None:
        store = SkillStore(self.tmp / "other.db")
        self.addCleanup(store.close)
        lines = sync_folders(store, [str(support.DEMO_SKILLS), str(self.tmp / "nope")])
        self.assertEqual(store.count(), 3)
        self.assertIn("not a folder", lines[1])

    def test_sync_lock_is_exclusive(self) -> None:
        from triage.catalog_link import _acquire_sync_lock, _release_sync_lock

        path = self.tmp / ".sync.lock"
        first = _acquire_sync_lock(path)
        self.assertIsNotNone(first)
        self.assertIsNone(_acquire_sync_lock(path))
        _release_sync_lock(first)
        again = _acquire_sync_lock(path)
        self.assertIsNotNone(again)
        _release_sync_lock(again)



class NativeSkillTests(unittest.TestCase):
    def test_skills_in_hermes_own_folders_are_not_registered_twice(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="triage-native-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        store = SkillStore(tmp / "catalog.db")
        self.addCleanup(store.close)
        import_tree(store, support.DEMO_SKILLS)
        ctx = FakeCtx()
        native = [Path(store.get("git-commit").source).parent]  # treat the demo folder as Hermes' own
        self.assertEqual(register_catalog_skills(ctx, store.list_skills(), native_roots=native), 0)
        self.assertEqual(register_catalog_skills(FakeCtx(), store.list_skills(), native_roots=[]), 3)


if __name__ == "__main__":
    unittest.main()
