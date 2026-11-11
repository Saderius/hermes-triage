"""Desktop chip approval is durable and does not depend on slash.exec."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path

import support
from triage.importer import import_tree
from triage.service import Runtime, Settings
from triage.store import SkillStore


def _load_plugin_api():
    if "fastapi" not in sys.modules:
        fastapi = types.ModuleType("fastapi")

        class APIRouter:
            def get(self, *_args, **_kwargs):
                return lambda func: func

            def post(self, *_args, **_kwargs):
                return lambda func: func

        class Query:
            def __init__(self, *_args, **_kwargs):
                pass

        fastapi.APIRouter = APIRouter
        fastapi.Query = Query
        sys.modules["fastapi"] = fastapi
    path = support.ROOT / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("triage_plugin_api", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DesktopApproveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="hsj-approve-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.store = SkillStore(self.tmp / "catalog.db")
        self.addCleanup(self.store.close)
        import_tree(self.store, support.DEMO_SKILLS)
        self.runtime = Runtime(self.store, settings=Settings())

    def test_pending_force_loads_on_the_next_turn(self) -> None:
        self.store.enqueue_force("desk", "git-commit", 0.61, "desktop approve")
        loaded = self.runtime.on_pre_llm(
            user_message="commit the staged files",
            session_id="desk",
            turn_id="t1",
            platform="desktop",
        )
        self.assertIn("Loaded skill git-commit", loaded["context"])
        self.assertIn("COMMIT_BODY_ONLY", loaded["context"])
        self.assertIsNone(self.runtime.on_pre_tool(
            tool_name="skill_view",
            args={"name": "git-commit"},
            session_id="desk",
            turn_id="t1",
        ))
        follow = self.runtime.on_pre_llm(
            user_message="what is the weather in lisbon",
            session_id="desk",
            turn_id="t2",
            platform="desktop",
        )
        self.assertNotIn("Loaded skill git-commit", (follow or {}).get("context") or "")
        self.assertEqual(self.runtime.settings().auto_accept_threshold, 0.90)

    def test_desktop_turn_without_a_pile_adds_no_list_and_loads_nothing(self) -> None:
        # The magazine is the only way a skill loads on desktop: no "pick one"
        # shortlist on the model, no "which skill?" question, no server-side
        # auto-accept. Hermes Desktop chats arrive as platform "local".
        for platform, session, marked in (("desktop", "d1", False), ("local", "l1", False), ("cli", "chips", True)):
            if marked:
                self.store.mark_chip_session(session)
            result = self.runtime.on_pre_llm(
                user_message="please run git-commit on the staged diff, write the git-commit message",
                session_id=session, turn_id=f"t-{session}", platform=platform,
            )
            self.assertIsNone(result)
        other = self.runtime.on_pre_llm(
            user_message="please run git-commit on the staged diff, write the git-commit message",
            session_id="telegram", turn_id="t-tg", platform="telegram",
        )
        self.assertIn("git-commit", (other or {}).get("context") or "")

    def test_unknown_approve_name_is_not_forced(self) -> None:
        self.store.enqueue_force("desk", "not-a-skill", 1.0, "desktop approve")
        decision = self.runtime.on_pre_llm(
            user_message="hello there",
            session_id="desk",
            turn_id="t1",
            platform="desktop",
        )
        if decision and decision.get("context"):
            self.assertNotIn("Loaded skill", decision["context"])

    def test_approve_helper_is_defined_and_queue_survives_without_runtime(self) -> None:
        api = _load_plugin_api()
        self.assertTrue(callable(api._try_live_force))
        self.assertFalse(api._try_live_force("desk", "git-commit"))

        catalog = SkillStore(self.tmp / "approve.db")
        import_tree(catalog, support.DEMO_SKILLS)
        original_store = api._store
        api._store = lambda: catalog
        try:
            result = asyncio.run(api.approve({"name": "git-commit", "session_id": "desk"}))
        finally:
            api._store = original_store
        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "git-commit")
        again = SkillStore(self.tmp / "approve.db")
        self.addCleanup(again.close)
        row = again.pop_pending_force("desk")
        self.assertIsNotNone(row)
        self.assertEqual(row["name"], "git-commit")

    def test_try_live_force_mirrors_in_process_runtime(self) -> None:
        api = _load_plugin_api()
        forced: dict = {}

        class Memory:
            def force(self, session_id, name, probability, why):
                forced.update(
                    session_id=session_id,
                    name=name,
                    probability=probability,
                    why=why,
                )

        class Live:
            memory = Memory()

        class Service:
            @staticmethod
            def current_runtime():
                return Live()

        class Down:
            @staticmethod
            def current_runtime():
                raise RuntimeError("down")

        original = api._mod
        api._mod = lambda name: Service if name == "service" else original(name)
        try:
            self.assertTrue(api._try_live_force("desk", "git-commit"))
            api._mod = lambda name: Down if name == "service" else original(name)
            self.assertFalse(api._try_live_force("desk", "git-commit"))
        finally:
            api._mod = original
        self.assertEqual(forced["name"], "git-commit")
        self.assertEqual(forced["why"], "desktop approve")
        self.assertEqual(forced["probability"], 0.0)

    def test_desktop_chip_click_holds_the_skill_without_touching_the_prompt(self) -> None:
        # docs/designs/how-triage-works.md rule 1: a click holds the skill on the
        # left pile (POST /attach); the prompt text is never edited or sent.
        text = (support.ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
        self.assertIn("ctx.rest('/attach'", text)
        self.assertNotIn("insertText(", text)
        # The one prompt edit: removing a "/term" the user typed to search,
        # after a search result is picked.
        self.assertEqual(text.count("composer.setDraft("), 1)
        clear = text[text.index("async function clearSlashTerm"):]
        self.assertIn("setDraft(", clear[: clear.index("commitRef.current = commit")])
        self.assertNotIn("composer.submit", text)
        self.assertNotIn("slash.exec", text)
        self.assertIn("maxRef.current", text)
        self.assertIn("AUTO_DETACH_MARGIN", text)
        self.assertIn("function Magazine", text)
        self.assertIn("flying.dx", text)

    def test_attach_endpoint_validates_caps_and_round_trips(self) -> None:
        api = _load_plugin_api()
        path = self.tmp / "attach.db"
        seeded = SkillStore(path)
        import_tree(seeded, support.DEMO_SKILLS)
        seeded.close()
        original_store = api._store
        api._store = lambda: SkillStore(path)
        try:
            result = asyncio.run(api.post_attach({"session_id": "desk", "names": ["git-commit", "nope", "git-commit"]}))
            self.assertEqual(result["names"], ["git-commit"])
            self.assertEqual(result["unknown"], ["nope"])
            self.assertEqual(asyncio.run(api.get_attach("desk"))["names"], ["git-commit"])
            asyncio.run(api.post_attach({"session_id": "desk", "names": []}))
            self.assertEqual(asyncio.run(api.get_attach("desk"))["names"], [])
        finally:
            api._store = original_store

    def turn(self, text, session="desk", first=False):
        return self.runtime.on_pre_llm(
            user_message=text, session_id=session, turn_id=f"t-{text[:8]}", platform="desktop",
            is_first_turn=first, conversation_history=[] if first else [{"role": "user", "content": "earlier"}],
        )

    def test_held_pile_loads_with_the_next_message_once(self) -> None:
        self.store.set_pending_attach("desk", ["git-commit", "pr-review"])
        result = self.turn("review the commit message")
        self.assertIn("Skills attached to this message", result["context"])
        self.assertIn("COMMIT_BODY_ONLY", result["context"])
        self.assertNotIn("probability", result["context"].lower())
        self.assertEqual(self.store.get_pending_attach("desk"), [])
        again = self.turn("and now something else")
        self.assertNotIn("Skills attached to this message", (again or {}).get("context") or "")

    def test_new_chat_takes_the_fresh_pile_on_its_first_turn(self) -> None:
        # Another chat's unsent pile is never taken by a new chat.
        self.store.set_pending_attach("other-chat", ["pr-review"])
        self.store.set_pending_attach("runtime-id", ["git-commit"], fresh=True)
        self.assertNotIn("COMMIT_BODY_ONLY", (self.turn("hello", session="other") or {}).get("context") or "")
        result = self.turn("commit this", session="local-new", first=True)
        self.assertIn("COMMIT_BODY_ONLY", result["context"])
        self.assertEqual(self.store.get_pending_attach("other-chat"), ["pr-review"])

    def test_held_pile_respects_max_attached(self) -> None:
        self.runtime = Runtime(self.store, settings=Settings(max_attached=1))
        self.store.set_pending_attach("desk", ["git-commit", "pr-review"])
        result = self.turn("review the commit message")
        self.assertEqual(result["context"].count("\n# "), 1)


if __name__ == "__main__":
    unittest.main()
