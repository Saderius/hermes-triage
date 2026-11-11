"""skill_view approval guard and plugin registration."""

from __future__ import annotations

import unittest
from pathlib import Path

import support
from triage.guard import evaluate_skill_view
from triage.importer import import_tree
from triage.plugin import register
from triage.service import Runtime, Settings
from triage.store import SkillStore


class GuardTests(unittest.TestCase):
    def test_blocks_unapproved_names_and_ignores_other_tools(self) -> None:
        blocked = evaluate_skill_view(
            "skill_view",
            {"name": "git-commit"},
            is_approved=lambda _name: False,
        )
        self.assertEqual(blocked["action"], "block")
        self.assertIn("git-commit", blocked["message"])

        missing = evaluate_skill_view("skill_view", {}, is_approved=lambda _name: True)
        self.assertEqual(missing["action"], "block")

        self.assertIsNone(
            evaluate_skill_view(
                "skill_view",
                {"name": "plugin:git-commit", "file_path": "notes.md"},
                is_approved=lambda name: name == "git-commit",
            )
        )
        self.assertIsNone(
            evaluate_skill_view("terminal", {"command": "rm -rf /"}, is_approved=lambda _name: False)
        )

    def test_approval_lasts_for_the_session(self) -> None:
        import tempfile

        tmp = Path(tempfile.mkdtemp(prefix="hsj-guard-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        store = SkillStore(tmp / "catalog.db")
        self.addCleanup(store.close)
        import_tree(store, support.DEMO_SKILLS)
        runtime = Runtime(store, settings=Settings(auto_accept_threshold=0.99))

        shortlist = runtime.on_pre_llm(
            user_message="review the commit message",
            turn_id="turn-a",
            session_id="s",
            platform="cli",
        )
        self.assertNotIn("Loaded skill", shortlist["context"])
        blocked = runtime.on_pre_tool(
            tool_name="skill_view",
            args={"name": "git-commit"},
            turn_id="turn-a",
            session_id="s",
        )
        self.assertEqual(blocked["action"], "block")

        loaded = runtime.on_pre_llm(user_message="1", turn_id="turn-b", session_id="s")
        self.assertIn("COMMIT_BODY_ONLY", loaded["context"])
        self.assertIsNone(
            runtime.on_pre_tool(
                tool_name="skill_view",
                args={"name": "git-commit", "file_path": "notes.md"},
                turn_id="turn-b",
                session_id="s",
            )
        )
        other = runtime.on_pre_tool(
            tool_name="skill_view",
            args={"name": "weather-brief"},
            turn_id="turn-b",
        )
        self.assertEqual(other["action"], "block")
        later = runtime.on_pre_tool(
            tool_name="skill_view",
            args={"name": "git-commit"},
            turn_id="turn-c",
            session_id="s",
        )
        self.assertIsNone(later)
        other_session = runtime.on_pre_tool(
            tool_name="skill_view",
            args={"name": "git-commit"},
            turn_id="turn-c",
            session_id="other-session",
        )
        self.assertEqual(other_session["action"], "block")

    def test_register_wires_hooks_middleware_and_judge(self) -> None:
        import tempfile

        tmp = Path(tempfile.mkdtemp(prefix="hsj-reg-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        db = tmp / "catalog.db"
        store = SkillStore(db)
        import_tree(store, support.DEMO_SKILLS)
        store.close()

        import triage.plugin as plugin

        plugin.default_db_path = lambda: db
        ctx = _FakeCtx()
        register(ctx)
        self.assertIn("pre_llm_call", ctx.hooks)
        self.assertIn("pre_tool_call", ctx.hooks)
        self.assertIn("llm_request", ctx.middleware)
        self.assertIn("triage", ctx.commands)
        self.assertIn("triage", ctx.cli)

        loaded = ctx.hooks["pre_llm_call"](
            user_message="please run git-commit on the staged diff",
            turn_id="reg-1",
            session_id="reg",
            platform="cli",
            is_first_turn=False,
        )
        self.assertIn("COMMIT_BODY_ONLY", loaded["context"])
        self.assertNotIn("WEATHER_BODY_ONLY", loaded["context"])
        self.assertIsNone(
            ctx.hooks["pre_tool_call"](
                tool_name="skill_view",
                args={"name": "git-commit"},
                turn_id="reg-1",
                session_id="reg",
            )
        )
        blocked = ctx.hooks["pre_tool_call"](
            tool_name="skill_view",
            args={"name": "pr-review"},
            turn_id="reg-1",
            session_id="reg",
        )
        self.assertEqual(blocked["action"], "block")


class _FakeCtx:
    def __init__(self) -> None:
        self.hooks = {}
        self.middleware = {}
        self.commands = {}
        self.cli = {}
        self.config = {}

    def register_hook(self, name, callback, **_kwargs):
        self.hooks[name] = callback

    def register_middleware(self, kind, callback):
        self.middleware[kind] = callback

    def register_command(self, name, handler, description="", **_kwargs):
        self.commands[name] = handler

    def register_cli_command(self, **kwargs):
        self.cli[kwargs["name"]] = kwargs

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def ask_choice(self, _question, _choices):
        return None


if __name__ == "__main__":
    unittest.main()
