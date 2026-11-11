"""Judge ranking, probabilities, auto-accept, and the embedder extension."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import support
from triage.importer import import_tree
from triage.judge import SkillJudge, load_embedder
from triage.service import Runtime, Settings
from triage.store import SkillStore


class JudgeTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="hsj-judge-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.store = SkillStore(self.tmp / "catalog.db")
        import_tree(self.store, support.DEMO_SKILLS)

    def tearDown(self) -> None:
        self.store.close()

    def test_ambiguous_query_ranks_commit_and_shows_probabilities(self) -> None:
        ranking = SkillJudge(self.store).rank("review the commit message")
        self.assertGreaterEqual(len(ranking.candidates), 2)
        self.assertEqual(ranking.candidates[0].name, "git-commit")
        self.assertLess(ranking.candidates[0].probability, 0.85)
        self.assertIn("pr-review", [item.name for item in ranking.candidates])
        total = ranking.none_probability + sum(item.probability for item in ranking.candidates)
        self.assertAlmostEqual(total, 1.0, places=2)
        for item in ranking.candidates:
            self.assertIn("%", item.label())
            self.assertGreater(item.probability, 0)

        runtime = Runtime(self.store, settings=Settings(auto_accept_threshold=0.85))
        decision = runtime.on_pre_llm(
            user_message="review the commit message",
            turn_id="t1",
            session_id="s",
            platform="cli",
        )
        self.assertIn("%", decision["context"])
        self.assertIn("git-commit", decision["context"])
        self.assertNotIn("Loaded skill", decision["context"])
        self.assertNotIn("COMMIT_BODY_ONLY", decision["context"])

    def test_short_skill_name_needs_a_whole_word(self) -> None:
        # Large catalogs ship skills named "c", "go", "dep". A substring test
        # let them claim any draft that contains those letters.
        self.store.upsert(
            name="dep",
            description="Containerization and deployment setup.",
            body="# dep\nShip it.",
            tags=[],
            source=str(self.tmp / "dep"),
            origin_path="dep/SKILL.md",
            content_hash="dep-test",
        )
        ranking = SkillJudge(self.store).rank("review the deployment of the commit message")
        dep = [item for item in ranking.candidates if item.name == "dep"]
        if dep:
            self.assertNotIn("name dep", dep[0].why)
        named = SkillJudge(self.store).rank("use dep for this")
        self.assertEqual(named.candidates[0].name, "dep")

    def test_explicit_name_auto_accepts_only_that_body(self) -> None:
        runtime = Runtime(self.store, settings=Settings(auto_accept_threshold=0.85))
        decision = runtime.on_pre_llm(
            user_message="please run git-commit on the staged diff",
            turn_id="t2",
            session_id="s",
            platform="cli",
        )
        self.assertIn("Loaded skill git-commit", decision["context"])
        self.assertIn("COMMIT_BODY_ONLY", decision["context"])
        self.assertNotIn("WEATHER_BODY_ONLY", decision["context"])
        self.assertNotIn("REVIEW_BODY_ONLY", decision["context"])
        self.assertIsNone(
            runtime.on_pre_tool(tool_name="skill_view", args={"name": "git-commit"}, turn_id="t2")
        )

    def test_number_pick_injects_the_shortlist_choice(self) -> None:
        runtime = Runtime(self.store, settings=Settings(auto_accept_threshold=0.99))
        first = runtime.on_pre_llm(
            user_message="review the commit message",
            turn_id="t3",
            session_id="sess",
            platform="cli",
        )
        self.assertLess(first["context"].index("git-commit"), first["context"].index("pr-review"))
        second = runtime.on_pre_llm(user_message="1", turn_id="t4", session_id="sess", platform="cli")
        self.assertIn("Loaded skill git-commit", second["context"])
        self.assertIn("COMMIT_BODY_ONLY", second["context"])
        self.assertNotIn("REVIEW_BODY_ONLY", second["context"])

    def test_skip_and_long_sentence_do_not_load_a_body(self) -> None:
        runtime = Runtime(self.store, settings=Settings(auto_accept_threshold=0.99))
        runtime.on_pre_llm(
            user_message="review the commit message",
            turn_id="t5",
            session_id="sess",
            platform="cli",
        )
        skipped = runtime.on_pre_llm(user_message="skip", turn_id="t6", session_id="sess")
        self.assertIn("No skill loaded", skipped["context"])
        self.assertNotIn("COMMIT_BODY_ONLY", skipped["context"])

        runtime.on_pre_llm(
            user_message="review the commit message",
            turn_id="t7",
            session_id="sess",
            platform="cli",
        )
        sentence = runtime.on_pre_llm(
            user_message="please review the commit message carefully",
            turn_id="t8",
            session_id="sess",
        )
        self.assertNotIn("Loaded skill", sentence["context"])

    def test_ask_choice_on_the_turn_injects_that_skill(self) -> None:
        def ask(_question, choices):
            self.assertTrue(any("%" in choice["label"] for choice in choices))
            return "weather-brief"

        runtime = Runtime(
            self.store,
            settings=Settings(auto_accept_threshold=0.99, interactive=True),
            ask_choice=ask,
        )
        decision = runtime.on_pre_llm(
            user_message="forecast rain in lisbon",
            turn_id="t9",
            session_id="s",
            platform="cli",
        )
        self.assertIn("WEATHER_BODY_ONLY", decision["context"])
        self.assertNotIn("COMMIT_BODY_ONLY", decision["context"])

    def test_hook_falls_back_when_ask_choice_returns_none(self) -> None:
        def ask(_question, _choices):
            return None

        runtime = Runtime(
            self.store,
            settings=Settings(auto_accept_threshold=0.99, interactive=True),
            ask_choice=ask,
        )
        decision = runtime.on_pre_llm(
            user_message="review the commit message",
            turn_id="t10",
            session_id="s",
            platform="cli",
        )
        self.assertIn("%", decision["context"])
        self.assertNotIn("Loaded skill", decision["context"])

    def test_slash_choice_loads_on_the_next_message(self) -> None:
        def ask(_question, choices):
            self.assertTrue(any("%" in choice["label"] for choice in choices))
            return next(choice["value"] for choice in choices if choice["value"] == "pr-review")

        runtime = Runtime(
            self.store,
            settings=Settings(auto_accept_threshold=0.99),
            ask_choice=ask,
        )
        reply = runtime.handle_command("review the commit message", session_id="slash")
        self.assertIn("Approved pr-review", reply)
        self.assertIn("%", reply)
        loaded = runtime.on_pre_llm(
            user_message="go ahead and review it",
            turn_id="t11",
            session_id="slash",
        )
        self.assertIn("REVIEW_BODY_ONLY", loaded["context"])
        self.assertNotIn("COMMIT_BODY_ONLY", loaded["context"])
        self.assertNotIn("WEATHER_BODY_ONLY", loaded["context"])

    def test_embedder_can_outrank_keywords(self) -> None:
        class BoostWeather:
            def score(self, _query, skill):
                return 1.0 if skill.name == "weather-brief" else 0.0

        ranking = SkillJudge(self.store, embedder=BoostWeather()).rank("zzzz qqqq unique")
        self.assertEqual(ranking.candidates[0].name, "weather-brief")
        self.assertIn("embed", ranking.candidates[0].why)

        sys.path.insert(0, str(support.ROOT / "examples"))
        loaded = load_embedder("constant_embedder.ConstantEmbedder")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.score("q", type("S", (), {"name": "weather-brief"})()), 1.0)
        self.assertIsNone(load_embedder(""))
        self.assertIsNone(load_embedder("missing.module.Nope"))

    def test_deep_body_sentence_outranks_a_name_hit(self) -> None:
        filler = "lorem ipsum dolor sit amet " * 500
        sentence = "The quartz ledger must stay append only during nightly compaction."
        self.store.upsert(
            name="storage-notes",
            description="Internal notes about storage.",
            body=filler + "\n" + sentence + "\n" + filler,
            tags=[],
            source="",
            origin_path="storage-notes/SKILL.md",
            content_hash="deep-body",
            refs="",
        )
        self.store.upsert(
            name="quartz-ledger",
            description="Ledger name tools for quartz.",
            body="A short skill that only matches the name.",
            tags=[],
            source="",
            origin_path="quartz-ledger/SKILL.md",
            content_hash="name-only",
            refs="",
        )
        ranking = SkillJudge(self.store).rank(sentence)
        self.assertEqual(ranking.candidates[0].name, "storage-notes")
        self.assertGreaterEqual(ranking.candidates[0].probability, 0.90)
        self.assertIn("body sentence", ranking.candidates[0].why)
        self.assertGreater(len(self.store.get("storage-notes").body), 4000)

    def test_reference_sentence_is_indexed(self) -> None:
        import tempfile

        root = Path(tempfile.mkdtemp(prefix="hsj-refs-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        skill = root / "ref-skill"
        (skill / "references").mkdir(parents=True)
        sentence = "Always rotate the violet cache key before publishing artifacts."
        (skill / "SKILL.md").write_text(
            "---\nname: ref-skill\ndescription: Packaging notes.\n---\n\nSee the reference.\n",
            encoding="utf-8",
        )
        (skill / "references" / "keys.md").write_text(sentence + "\n", encoding="utf-8")
        import_tree(self.store, root)
        stored = self.store.get("ref-skill")
        self.assertIn("violet cache key", stored.refs)
        ranking = SkillJudge(self.store).rank(sentence)
        self.assertEqual(ranking.candidates[0].name, "ref-skill")

    def test_latest_user_turn_beats_a_long_assistant_dump(self) -> None:
        history = [
            {"role": "user", "content": "what is the weather in lisbon tomorrow"},
            {
                "role": "assistant",
                "content": " ".join(["forecast rain wind temperature lisbon"] * 400),
            },
        ]
        ranking = SkillJudge(self.store).rank(
            "please write a commit message for the staged diff",
            history=history,
            thoughts="keep talking about the lisbon forecast and the rain",
        )
        self.assertEqual(ranking.candidates[0].name, "git-commit")
        runtime = Runtime(self.store, settings=Settings(auto_accept_threshold=0.99))
        decision = runtime.on_pre_llm(
            user_message="please write a commit message for the staged diff",
            conversation_history=history,
            thoughts="lisbon forecast rain",
            turn_id="multi",
            session_id="multi",
            platform="cli",
        )
        self.assertLess(
            decision["context"].index("git-commit"),
            decision["context"].index("weather-brief") if "weather-brief" in decision["context"] else 10**9,
        )

    def test_paraphrase_uses_embedder_when_present(self) -> None:
        class ShipEmbedder:
            def score(self, query, skill):
                if "ship the change" in query and skill.name == "git-commit":
                    return 0.93
                return 0.0

        fused = SkillJudge(self.store, embedder=ShipEmbedder()).rank("ship the change please")
        self.assertEqual(fused.candidates[0].name, "git-commit")
        self.assertIn("rrf", fused.candidates[0].why)
        lexical = SkillJudge(self.store).rank("ship the change please")
        self.assertTrue(lexical.candidates == [] or lexical.candidates[0].name != "git-commit" or lexical.candidates[0].probability < 0.95)

    def _topic_skills(self) -> None:
        specs = {
            "android-debug": (
                "Debug Android Kotlin apps with Logcat and the activity lifecycle.",
                "Filter Logcat by tag. Inspect the Kotlin activity lifecycle when a crash repeats.",
            ),
            "nodejs-api": (
                "Express npm API routes and async handlers.",
                "An Express route should await the async handler and surface npm errors.",
            ),
            "hermes-agent": (
                "Hermes agent plugin and system prompt internals.",
                "The hermes agent loads plugins, builds the system prompt, and records skill usage. " * 40,
            ),
        }
        for name, (description, body) in specs.items():
            self.store.upsert(
                name=name,
                description=description,
                body=body,
                tags=[],
                source="",
                origin_path=f"{name}/SKILL.md",
                content_hash=name,
                refs="",
            )

    def test_android_node_android_return_follows_the_latest_user(self) -> None:
        self._topic_skills()
        history = [
            {"role": "user", "content": "debug this Android Kotlin Logcat crash in the activity"},
            {"role": "assistant", "content": " ".join(["android kotlin logcat activity gradle"] * 80)},
            {"role": "user", "content": "switch to the Express npm API and the async handler"},
            {"role": "assistant", "content": " ".join(["express npm async route handler"] * 80)},
        ]
        ranking = SkillJudge(self.store).rank(
            "back to Kotlin Logcat and the activity lifecycle",
            history=history,
        )
        self.assertEqual(ranking.candidates[0].name, "android-debug")
        names = [item.name for item in ranking.candidates]
        if "hermes-agent" in names:
            self.assertLess(names.index("android-debug"), names.index("hermes-agent"))
        if "nodejs-api" in names:
            self.assertLess(names.index("android-debug"), names.index("nodejs-api"))
        self.assertLess(ranking.candidates[0].probability, 0.95)

    def test_weather_ask_beats_heavy_android_history(self) -> None:
        self._topic_skills()
        history = [
            {"role": "user", "content": "debug this Android Kotlin Logcat crash"},
            {"role": "assistant", "content": " ".join(["android kotlin logcat gradle activity lifecycle"] * 120)},
            {"role": "user", "content": "still the Android activity and Logcat tags"},
            {"role": "assistant", "content": " ".join(["Logcat Android Kotlin activity"] * 120)},
        ]
        ranking = SkillJudge(self.store).rank(
            "what's the weather in lisbon tomorrow",
            history=history,
        )
        self.assertEqual(ranking.candidates[0].name, "weather-brief")
        by_name = {item.name: item for item in ranking.candidates}
        if "android-debug" in by_name:
            self.assertLess(by_name["android-debug"].probability, by_name["weather-brief"].probability)
            self.assertLess(by_name["android-debug"].probability, 0.4)
            self.assertNotIn("assistant context", by_name["android-debug"].why)
        self.assertLess(ranking.candidates[0].probability, 0.95)

    def test_default_threshold_stays_strict_on_fixtures(self) -> None:
        runtime = Runtime(self.store)
        self.assertEqual(runtime.settings().auto_accept_threshold, 0.90)
        weak = runtime.on_pre_llm(
            user_message="review the commit message",
            turn_id="weak",
            session_id="thr",
            platform="cli",
        )
        self.assertNotIn("Loaded skill", weak["context"])
        strong = runtime.on_pre_llm(
            user_message="please run git-commit on the staged diff",
            turn_id="strong",
            session_id="thr",
            platform="cli",
        )
        self.assertIn("Loaded skill git-commit", strong["context"])
        self.assertIn("COMMIT_BODY_ONLY", strong["context"])


if __name__ == "__main__":
    unittest.main()
