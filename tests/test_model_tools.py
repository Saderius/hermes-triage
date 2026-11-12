"""skill_search / skill_load: the model finds and loads skills itself."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import support
from triage.importer import import_tree
from triage.model_tools import register_model_tools, skill_load, skill_search
from triage.service import Runtime, Settings, install_runtime
from triage.store import SkillStore


class ModelToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="triage-tools-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.store = SkillStore(self.tmp / "catalog.db")
        self.addCleanup(self.store.close)
        import_tree(self.store, support.DEMO_SKILLS)
        self.runtime = Runtime(self.store, settings=Settings())
        install_runtime(self.runtime)

    def test_search_lists_without_loading_then_load_returns_the_body_once(self) -> None:
        found = json.loads(skill_search({"query": "commit message"}, session_id="s"))
        names = [row["name"] for row in found["results"]]
        self.assertIn("git-commit", names)
        self.assertFalse(self.runtime.ledger.allows("", "git-commit", "s"))
        loaded = json.loads(skill_load({"names": ["git-commit", "nope"]}, session_id="s"))
        self.assertEqual(loaded["loaded"][0]["name"], "git-commit")
        self.assertIn("COMMIT_BODY_ONLY", loaded["loaded"][0]["instructions"])
        self.assertIn("nope", loaded["unknown"])
        self.assertTrue(self.runtime.ledger.allows("", "git-commit", "s"))
        again = json.loads(skill_load({"names": ["git-commit"]}, session_id="s"))
        self.assertEqual(again["loaded"], [])
        self.assertEqual(again["already_loaded"], ["git-commit"])
        marked = json.loads(skill_search({"query": "git-commit"}, session_id="s"))
        self.assertTrue(next(row for row in marked["results"] if row["name"] == "git-commit")["loaded"])

    def test_category_path_names_mean_the_skill(self) -> None:
        # Models address Hermes' skills by folder path: "category\\name" or "category/name".
        blocked = self.runtime.on_pre_tool(
            tool_name="skill_view", args={"name": "dev\\git-commit"}, session_id="s", turn_id="t1"
        )
        self.assertEqual(blocked["action"], "block")
        loaded = json.loads(skill_load({"names": ["dev\\git-commit"]}, session_id="s"))
        self.assertEqual(loaded["loaded"][0]["name"], "git-commit")
        self.assertNotIn("unknown", loaded)
        for name in ("dev\\git-commit", "dev/git-commit", "Git-Commit"):
            self.assertIsNone(
                self.runtime.on_pre_tool(tool_name="skill_view", args={"name": name}, session_id="s", turn_id="t2"),
                name,
            )
        viewed = json.loads(self.runtime.serve_skill_view({"name": "dev/git-commit", "file_path": "SKILL.md"}, session_id="s"))
        self.assertIn("COMMIT_BODY_ONLY", json.dumps(viewed))

    def test_registration_uses_its_own_toolset(self) -> None:
        seen = []

        class Ctx:
            def register_tool(self, **kwargs):
                seen.append((kwargs["name"], kwargs["toolset"]))

        self.assertTrue(register_model_tools(Ctx()))
        self.assertEqual(seen, [("skill_search", "triage"), ("skill_load", "triage")])


if __name__ == "__main__":
    unittest.main()
