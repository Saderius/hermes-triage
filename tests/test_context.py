"""Conversation-driven chips: state.db turns, first prompt, workspace AGENTS.md."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import support
from triage.context import chip_ranking, session_context, strip_injected
from triage.importer import import_tree
from triage.judge import SkillJudge
from triage.store import SkillStore


def make_state_db(path: Path, session_id: str, cwd: str, rows: list[tuple[str, str, str | None]]) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT);
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,
            content TEXT, api_content TEXT, active INTEGER NOT NULL DEFAULT 1
        );
        """
    )
    conn.execute("INSERT INTO sessions (id, cwd) VALUES (?, ?)", (session_id, cwd))
    for role, content, api in rows:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, api_content) VALUES (?, ?, ?, ?)",
            (session_id, role, content, api),
        )
    conn.commit()
    conn.close()


class ContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="triage-ctx-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.store = SkillStore(self.tmp / "catalog.db")
        self.addCleanup(self.store.close)
        import_tree(self.store, support.DEMO_SKILLS)
        self.judge = SkillJudge(self.store)
        self.workspace = self.tmp / "repo"
        self.workspace.mkdir()
        self.db = self.tmp / "state.db"

    def test_strip_injected_cuts_plugin_blocks(self) -> None:
        text = "explain this repo\n\n[triage] The system-prompt skills index is withheld.\n1. 40% git-commit"
        self.assertEqual(strip_injected(text), "explain this repo")
        self.assertEqual(strip_injected("hi\n\n## Retrieved Skills\n- x"), "hi")

    def test_own_words_drops_hermes_notes(self) -> None:
        from triage.context import own_words

        self.assertEqual(
            own_words(
                "[Context from the interrupted assistant response]\n"
                "[This response was interrupted by a user correction.]\n\n\"/reload-mcp is not shown\""
            ),
            '"/reload-mcp is not shown"',
        )
        self.assertEqual(
            own_words("[System: The active model for this chat has changed to x via provider y.]\n\ncontinue"),
            "continue",
        )
        self.assertEqual(own_words("[System: The active model for this chat has changed.]"), "")
        self.assertEqual(own_words("[PRIOR CONTEXT — for reference only; not a new message] The real reply"), "The real reply")
        skill = '[IMPORTANT: The user has invoked the "firebase" skill, indicating ...]\n\n---\nname: firebase\nbody'
        self.assertEqual(own_words(skill), "")
        self.assertEqual(
            own_words(skill + "\n\nThe user has provided the following instruction alongside the skill invocation: pull new issues"),
            "pull new issues",
        )
        note = (
            "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below. context handoff\n"
            "## Historical Task Snapshot\nsure\n## Goal\nFix premium sync phone to watch\n## Active State\nbranch x\n"
        )
        self.assertEqual(own_words(note), "Fix premium sync phone to watch")
        self.assertEqual(own_words("plain question [with brackets] inside"), "plain question [with brackets] inside")

    def test_session_context_skips_notes_after_a_compression(self) -> None:
        make_state_db(
            self.db,
            "s",
            str(self.workspace),
            [
                ("user", "[System: The active model for this chat has changed.]", None),
                ("assistant", "[PRIOR CONTEXT — for reference only; not a new message] checked the forecast", None),
                ("user", "rain tomorrow?", "[Context from the interrupted assistant response]\n\nrain tomorrow?"),
            ],
        )
        ctx = session_context("s", db_path=self.db)
        self.assertEqual(ctx.first_user, "rain tomorrow?")
        self.assertEqual(ctx.users, ["rain tomorrow?"])
        self.assertEqual(ctx.assistants, ["checked the forecast"])

    def test_session_context_reads_turns_and_cwd(self) -> None:
        make_state_db(
            self.db,
            "s",
            str(self.workspace),
            [
                ("user", "first question", "first question\n\n[hermes-skill-judge] shortlist..."),
                ("assistant", "an answer", None),
                ("tool", '{"output": "ls"}', None),
                ("user", "second question", None),
            ],
        )
        ctx = session_context("s", db_path=self.db)
        self.assertEqual(ctx.first_user, "first question")
        self.assertEqual(ctx.latest_user, "second question")
        self.assertEqual(ctx.assistants, ["an answer"])
        self.assertEqual(ctx.cwd, str(self.workspace))

    def test_empty_draft_keeps_conversation_chips(self) -> None:
        (self.workspace / "AGENTS.md").write_text(
            "This project prints a weather forecast brief every morning: temperature, rain, wind.",
            encoding="utf-8",
        )
        make_state_db(
            self.db,
            "s",
            str(self.workspace),
            [("user", "what does this repo do", None), ("assistant", "It writes a daily weather forecast brief.", None)],
        )
        result = chip_ranking(self.judge, "", "s", db_path=self.db)
        names = [row["name"] for row in result["candidates"]]
        self.assertIn("weather-brief", names)
        self.assertTrue(all(row["source"] == "conversation" for row in result["candidates"]))

    def test_draft_chips_come_first_and_conversation_keeps_a_slot(self) -> None:
        make_state_db(
            self.db,
            "s",
            str(self.workspace),
            [("user", "weather forecast brief for tomorrow", None), ("assistant", "Rain and wind expected.", None)],
        )
        result = chip_ranking(self.judge, "review the commit message", "s", db_path=self.db)
        rows = result["candidates"]
        self.assertEqual(rows[0]["source"], "draft")
        self.assertEqual(rows[0]["name"], "git-commit")
        self.assertIn("weather-brief", [row["name"] for row in rows if row["source"] == "conversation"])

    def test_unknown_session_still_ranks_the_draft(self) -> None:
        result = chip_ranking(self.judge, "review the commit message", "missing", db_path=self.db)
        self.assertEqual(result["candidates"][0]["name"], "git-commit")


if __name__ == "__main__":
    unittest.main()


class ChunkAndHeldTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="triage-chunks-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.store = SkillStore(self.tmp / "catalog.db")
        self.addCleanup(self.store.close)
        import_tree(self.store, support.DEMO_SKILLS)
        self.judge = SkillJudge(self.store)

    def test_draft_chunks_split_sentences_and_long_runs(self) -> None:
        from triage.context import draft_chunks

        self.assertEqual(draft_chunks("fix it"), ["fix it"])
        chunks = draft_chunks("review the pr please. then " + " ".join(f"w{i}" for i in range(23)))
        self.assertEqual(chunks[0], "review the pr please.")
        self.assertTrue(all(len(chunk.split()) <= 13 for chunk in chunks))
        self.assertLessEqual(len(draft_chunks(". ".join(f"s{i} a b" for i in range(20)))), 6)

    def test_each_topic_of_a_long_draft_gets_its_skill(self) -> None:
        result = chip_ranking(
            self.judge, "write the git-commit message. then the weather-brief for tomorrow.", "", limit=4, db_path=self.tmp / "none.db"
        )
        names = [row["name"] for row in result["candidates"]]
        self.assertIn("git-commit", names)
        self.assertIn("weather-brief", names)

    def test_independent_scores_let_two_skills_both_score_high(self) -> None:
        ranking = self.judge.rank("git-commit and pr-review", limit=4, independent=True)
        high = [item.name for item in ranking.candidates if item.probability >= 0.5]
        self.assertGreaterEqual(len(high), 2)
        shared = self.judge.rank("git-commit and pr-review", limit=4)
        self.assertLessEqual(sum(item.probability for item in shared.candidates), 1.0001)

    def test_held_and_excluded_skills_never_come_back(self) -> None:
        result = chip_ranking(
            self.judge, "git-commit pr-review weather-brief", "", limit=4,
            db_path=self.tmp / "none.db", held=["git-commit"], exclude=["git-commit", "pr-review"],
        )
        names = [row["name"] for row in result["candidates"]]
        self.assertNotIn("git-commit", names)
        self.assertNotIn("pr-review", names)
        self.assertIn("weather-brief", names)

    def test_near_duplicate_of_a_held_skill_is_dropped_unless_very_likely(self) -> None:
        from types import SimpleNamespace

        from triage.context import _keep, skill_similarity

        held = SimpleNamespace(name="android-dev", description="Android app development with Kotlin and Gradle")
        twin = SimpleNamespace(name="android-dev-kit", description="Android app development with Kotlin and Gradle", probability=0.8)
        other = SimpleNamespace(name="play-store", description="Publish releases to Google Play", probability=0.8)
        self.assertGreaterEqual(skill_similarity(held, twin), 0.7)
        self.assertFalse(_keep(twin, set(), [held]))
        self.assertTrue(_keep(other, set(), [held]))
        sure = SimpleNamespace(name=twin.name, description=twin.description, probability=0.97)
        self.assertTrue(_keep(sure, set(), [held]))


class TypoAndDuplicateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="triage-typo-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.store = SkillStore(self.tmp / "catalog.db")
        self.addCleanup(self.store.close)
        import_tree(self.store, support.DEMO_SKILLS)

    def test_unknown_words_snap_to_skill_words_known_words_stay(self) -> None:
        from triage.judge import fix_typos

        self.assertEqual(fix_typos("the weathr brief", self.store), "the weather brief")
        self.assertEqual(fix_typos("commit the weather", self.store), "commit the weather")
        self.assertEqual(fix_typos("zzzqqq", self.store), "zzzqqq")

    def test_inlined_skill_is_not_served_twice(self) -> None:
        import json

        from triage.service import Runtime, Settings

        runtime = Runtime(self.store, settings=Settings())
        self.store.set_pending_attach("s", ["git-commit"])
        runtime.on_pre_llm(user_message="go", session_id="s", turn_id="t1", platform="desktop",
                           is_first_turn=False, conversation_history=[{"role": "user", "content": "x"}])
        served = json.loads(runtime.serve_skill_view({"name": "git-commit"}, turn_id="t1", session_id="s"))
        self.assertIn("Already loaded", served["content"])
        self.assertNotIn("COMMIT_BODY_ONLY", served["content"])
        runtime.on_post_compaction(session_id="s")
        again = runtime.serve_skill_view({"name": "git-commit"}, turn_id="t2", session_id="s")
        self.assertIn("COMMIT_BODY_ONLY", again or "")


class HeldScoreTests(unittest.TestCase):
    def test_held_skills_report_their_score_without_coming_back_as_chips(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="triage-held-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        store = SkillStore(tmp / "catalog.db")
        self.addCleanup(store.close)
        import_tree(store, support.DEMO_SKILLS)
        result = chip_ranking(
            SkillJudge(store), "write the git-commit message", "", limit=4,
            db_path=tmp / "none.db", held=["git-commit"], exclude=["git-commit"],
        )
        self.assertNotIn("git-commit", [row["name"] for row in result["candidates"]])
        self.assertGreaterEqual(result["held_scores"].get("git-commit", 0), 0.9)


class SpareTests(unittest.TestCase):
    def test_spares_are_next_best_and_never_repeat_the_visible_chips(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="triage-spare-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        store = SkillStore(tmp / "catalog.db")
        self.addCleanup(store.close)
        import_tree(store, support.DEMO_SKILLS)
        result = chip_ranking(SkillJudge(store), "git-commit pr-review weather-brief", "", limit=1,
                              db_path=tmp / "none.db", spare=5)
        shown = [row["name"] for row in result["candidates"]]
        extra = [row["name"] for row in result["spare"]]
        self.assertEqual(len(shown), 1)
        self.assertGreaterEqual(len(extra), 1)
        self.assertFalse(set(shown) & set(extra))
