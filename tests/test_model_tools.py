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

    def test_registration_uses_its_own_toolset(self) -> None:
        seen = []

        class Ctx:
            def register_tool(self, **kwargs):
                seen.append((kwargs["name"], kwargs["toolset"]))

        self.assertTrue(register_model_tools(Ctx()))
        self.assertEqual(seen, [("skill_search", "triage"), ("skill_load", "triage")])


if __name__ == "__main__":
    unittest.main()
