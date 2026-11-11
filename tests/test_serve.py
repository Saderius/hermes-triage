"""Approved skill_view and skill_manage after the skills root is gone."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import support
from triage.catalog_io import HOOK_CONTEXT_SAFE_CHARS
from triage.importer import import_tree
from triage.plugin import install_runtime, tool_execution, transform_tool_result
from triage.service import Runtime, Settings
from triage.store import SkillStore


class ServeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="hsj-serve-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.src = self.tmp / "skills"
        self.skill_dir = self.src / "big-skill"
        self.skill_dir.mkdir(parents=True)
        marker = "UNIQUE_MIDDLE_MARKER"
        body = ("A" * 4500) + marker + ("B" * 4500)
        (self.skill_dir / "SKILL.md").write_text(
            "---\nname: big-skill\ndescription: A large imported skill.\n---\n\n" + body + "\n",
            encoding="utf-8",
        )
        (self.skill_dir / "references").mkdir()
        (self.skill_dir / "references" / "async.md").write_text(
            "LINKED_ASYNC_MARKER\n",
            encoding="utf-8",
        )
        self.store = SkillStore(self.tmp / "catalog.db")
        self.addCleanup(self.store.close)
        report = import_tree(self.store, self.src, copy=True)  # migrate keeps a copy
        self.assertIn("big-skill", report.inserted)
        shutil.rmtree(self.src)
        self.runtime = Runtime(self.store, settings=Settings(auto_accept_threshold=0.95))
        install_runtime(self.runtime)

    def test_empty_roots_still_serve_body_and_linked_file(self) -> None:
        self.assertEqual(list(self.src.rglob("SKILL.md")), [])
        skill = self.store.get("big-skill")
        mirror = Path(skill.source)
        self.assertTrue((mirror / "SKILL.md").is_file())
        self.assertTrue((mirror / "references" / "async.md").is_file())
        self.assertNotIn("skills", mirror.parts[-2:])  # mirror lives under plugin-data, not the emptied root
        self.assertIn("mirrors", mirror.parts)

        decision = self.runtime.on_pre_llm(
            user_message="please run big-skill on this task",
            turn_id="turn-1",
            session_id="sess",
            platform="cli",
        )
        context = decision["context"]
        self.assertLess(len(context), HOOK_CONTEXT_SAFE_CHARS)
        self.assertNotIn("UNIQUE_MIDDLE_MARKER", context)
        self.assertNotIn("plugin hook output truncated", context)
        self.assertIn("skill_view", context)
        self.assertIn("references/async.md", context)

        self.assertIsNone(self.runtime.on_pre_tool(
            tool_name="skill_view",
            args={"name": "big-skill"},
            turn_id="turn-1",
            session_id="sess",
        ))

        def core_must_not_run(_payload=None):
            raise AssertionError("Hermes skill_view ran against empty roots")

        served = tool_execution(
            tool_name="skill_view",
            args={"name": "big-skill"},
            next_call=core_must_not_run,
            turn_id="turn-1",
            session_id="sess",
        )
        payload = json.loads(served)
        self.assertTrue(payload["success"])
        self.assertIn("UNIQUE_MIDDLE_MARKER", payload["content"])
        self.assertGreater(len(payload["content"]), HOOK_CONTEXT_SAFE_CHARS)
        self.assertIn("references/async.md", payload["linked_files"]["references"])
        self.assertEqual(payload["served_by"], "triage")

        linked = tool_execution(
            tool_name="skill_view",
            args={"name": "big-skill", "file_path": "references/async.md"},
            next_call=core_must_not_run,
            turn_id="turn-2",
            session_id="sess",
        )
        linked_payload = json.loads(linked)
        self.assertTrue(linked_payload["success"])
        self.assertIn("LINKED_ASYNC_MARKER", linked_payload["content"])

        self.assertIsNone(self.runtime.on_pre_tool(
            tool_name="skill_view",
            args={"name": "big-skill", "file_path": "references/async.md"},
            turn_id="turn-2",
            session_id="sess",
        ))
        blocked = self.runtime.on_pre_tool(
            tool_name="skill_view",
            args={"name": "big-skill"},
            turn_id="turn-2",
            session_id="other",
        )
        self.assertEqual(blocked["action"], "block")
        unapproved = self.runtime.on_pre_tool(
            tool_name="skill_view",
            args={"name": "not-loaded"},
            turn_id="turn-2",
            session_id="sess",
        )
        self.assertEqual(unapproved["action"], "block")

    def test_transform_rewrites_core_not_found(self) -> None:
        self.runtime.ledger.approve("turn:t", ["big-skill"], "sess")
        replaced = transform_tool_result(
            tool_name="skill_view",
            args={"name": "big-skill", "file_path": "references/async.md"},
            result='{"success": false, "error": "Skill \'big-skill\' not found."}',
            turn_id="t",
            session_id="sess",
        )
        self.assertIsNotNone(replaced)
        payload = json.loads(replaced)
        self.assertIn("LINKED_ASYNC_MARKER", payload["content"])
        self.assertIsNone(transform_tool_result(
            tool_name="skill_view",
            args={"name": "not-loaded"},
            result='{"success": false, "error": "Skill \'not-loaded\' not found."}',
            turn_id="t",
            session_id="sess",
        ))

    def test_skill_manage_round_trip_stays_in_the_mirror(self) -> None:
        self.runtime.ledger.approve("turn:t", ["big-skill"], "sess")

        def core_must_not_run(_payload=None):
            raise AssertionError("skill_manage reached Hermes roots")

        written = json.loads(tool_execution(
            tool_name="skill_manage",
            args={
                "action": "write_file",
                "name": "big-skill",
                "file_path": "references/extra.md",
                "file_content": "EXTRA_FILE_MARKER\n",
            },
            next_call=core_must_not_run,
            session_id="sess",
        ))
        self.assertTrue(written["success"])
        extra = json.loads(self.runtime.serve_skill_view(
            {"name": "big-skill", "file_path": "references/extra.md"},
            session_id="sess",
        ))
        self.assertIn("EXTRA_FILE_MARKER", extra["content"])

        patched = json.loads(self.runtime.serve_skill_manage({
            "action": "patch",
            "name": "big-skill",
            "old_string": "UNIQUE_MIDDLE_MARKER",
            "new_string": "PATCHED_MARKER",
        }, session_id="sess"))
        self.assertTrue(patched["success"])
        viewed = json.loads(self.runtime.serve_skill_view({"name": "big-skill"}, session_id="sess"))
        self.assertIn("PATCHED_MARKER", viewed["content"])
        self.assertNotIn("UNIQUE_MIDDLE_MARKER", viewed["content"])

        # A name Triage does not index, and any create, belong to Hermes: the
        # call goes through to Hermes' own skill_manage, and this chat may use
        # the skill it made.
        self.assertIsNone(self.runtime.serve_skill_manage({
            "action": "write_file",
            "name": "other-skill",
            "file_path": "references/nope.md",
            "file_content": "nope",
        }, session_id="sess"))
        calls = []

        def hermes(_payload=None):
            calls.append(1)
            return json.dumps({"success": True, "message": "created by Hermes"})

        made = json.loads(tool_execution(
            tool_name="skill_manage",
            args={"action": "create", "name": "fresh-skill", "content": "---\nname: fresh-skill\n---\nx"},
            next_call=hermes,
            session_id="sess",
        ))
        self.assertEqual(calls, [1])
        self.assertEqual(made["message"], "created by Hermes")
        self.assertTrue(self.runtime.ledger.allows("", "fresh-skill", "sess"))

    def test_delete_never_removes_a_folder_triage_only_indexes(self) -> None:
        own = self.tmp / "own-skills" / "kept-skill"
        own.mkdir(parents=True)
        (own / "SKILL.md").write_text("---\nname: kept-skill\ndescription: Mine.\n---\n\nBODY\n", encoding="utf-8")
        import_tree(self.store, own.parent)
        self.runtime.ledger.approve("turn:t", ["kept-skill"], "sess")
        result = json.loads(self.runtime.serve_skill_manage({"action": "delete", "name": "kept-skill"}, session_id="sess"))
        self.assertFalse(result["success"])
        self.assertTrue((own / "SKILL.md").is_file())

    def test_edit_changes_the_original_file_where_the_skill_lives(self) -> None:
        own = self.tmp / "own-skills" / "live-skill"
        own.mkdir(parents=True)
        (own / "SKILL.md").write_text("---\nname: live-skill\ndescription: Mine.\n---\n\nOLD_RULE\n", encoding="utf-8")
        import_tree(self.store, own.parent)
        self.runtime.ledger.approve("turn:t", ["live-skill"], "sess")
        result = json.loads(self.runtime.serve_skill_manage(
            {"action": "patch", "name": "live-skill", "old_string": "OLD_RULE", "new_string": "NEW_RULE"},
            session_id="sess",
        ))
        self.assertTrue(result["success"], result)
        self.assertIn("NEW_RULE", (own / "SKILL.md").read_text(encoding="utf-8"))
        self.assertIn("NEW_RULE", self.store.get("live-skill").body)
        self.assertFalse((self.tmp / "mirrors" / "live-skill").exists())


if __name__ == "__main__":
    unittest.main()
