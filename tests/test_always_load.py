"""Always-load: global setting + per-project file, applied on the first prompt."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import support
from triage.importer import import_tree
from triage.preload import parse_names, project_names
from triage.service import Runtime, Settings
from triage.store import SkillStore


class AlwaysLoadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="hsj-always-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.store = SkillStore(self.tmp / "catalog.db")
        self.addCleanup(self.store.close)
        import_tree(self.store, support.DEMO_SKILLS)
        self.store.upsert(
            name="hermes-agent",
            description="Harness guide.",
            body="HARNESS_BODY " + ("x" * 9000),
            tags=[],
            source="",
            origin_path="hermes-agent/SKILL.md",
            content_hash="harness",
            refs="",
        )
        self.project = self.tmp / "repo"
        (self.project / ".hermes").mkdir(parents=True)
        (self.project / "src" / "deep").mkdir(parents=True)

    def runtime(self, always_load=()) -> Runtime:
        return Runtime(self.store, settings=Settings(always_load=tuple(always_load)))

    def turn(self, runtime, text, *, first, session="s", cwd=""):
        return runtime.on_pre_llm(
            user_message=text,
            turn_id=f"t-{text}",
            session_id=session,
            platform="desktop",
            is_first_turn=first,
            conversation_history=[] if first else [{"role": "user", "content": "earlier"}],
            cwd=cwd,
        )

    def test_parse_names_accepts_strings_and_lists(self) -> None:
        self.assertEqual(parse_names("git-commit, pr-review\nweather-brief"), ["git-commit", "pr-review", "weather-brief"])
        self.assertEqual(parse_names(["a", " b ", ""]), ["a", "b"])
        self.assertEqual(parse_names(None), [])

    def test_global_list_loads_on_first_prompt_only(self) -> None:
        runtime = self.runtime(["git-commit"])
        first = self.turn(runtime, "hello there", first=True)
        self.assertIn("Always-load skills", first["context"])
        self.assertIn("COMMIT_BODY_ONLY", first["context"])
        self.assertTrue(runtime.ledger.allows("other-scope", "git-commit", "s"))
        second = self.turn(runtime, "hello again", first=False)
        self.assertNotIn("Always-load skills", (second or {}).get("context", ""))

    def test_project_file_found_from_subfolder_and_merged_after_global(self) -> None:
        (self.project / ".hermes" / "triage.yaml").write_text(
            "always_load:\n  - pr-review\n  - git-commit  # dup of global\n", encoding="utf-8"
        )
        names, path = project_names(self.project / "src" / "deep")
        self.assertEqual(names, ["pr-review", "git-commit"])
        self.assertIsNotNone(path)
        runtime = self.runtime(["git-commit"])
        self.assertEqual(runtime.always_load_names(str(self.project / "src"))[0], ["git-commit", "pr-review"])
        context = self.turn(runtime, "hi", first=True, cwd=str(self.project / "src"))["context"]
        self.assertIn("COMMIT_BODY_ONLY", context)
        self.assertIn("triage.yaml", context)

    def test_large_body_goes_through_skill_view_and_unknown_names_are_reported(self) -> None:
        runtime = self.runtime(["hermes-agent", "no-such-skill"])
        context = self.turn(runtime, "hi", first=True)["context"]
        self.assertNotIn("HARNESS_BODY", context)
        self.assertIn("skill_view(name='hermes-agent')", context)
        self.assertIn("no-such-skill", context)
        self.assertTrue(runtime.ledger.allows("x", "hermes-agent", "s"))

    def test_reapplied_after_compaction(self) -> None:
        runtime = self.runtime(["git-commit"])
        self.turn(runtime, "hi", first=True)
        runtime.on_post_compaction(session_id="s", old_session_id="")
        after = self.turn(runtime, "continue", first=False)
        self.assertIn("COMMIT_BODY_ONLY", after["context"])
        later = self.turn(runtime, "more", first=False)
        self.assertNotIn("Always-load skills", (later or {}).get("context", ""))

    def test_empty_lists_change_nothing(self) -> None:
        runtime = self.runtime()
        result = self.turn(runtime, "what is the weather", first=True)
        self.assertNotIn("Always-load", (result or {}).get("context", ""))


if __name__ == "__main__":
    unittest.main()
