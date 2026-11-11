"""System-prompt skills-index strip and llm_request middleware."""

from __future__ import annotations

import copy
import unittest

import support  # noqa: F401
from triage.plugin import llm_request
from triage.service import Runtime, Settings, install_runtime
from triage.store import SkillStore
from triage.strip import strip_provider_request, strip_skills_index

# Shortened from agent/prompt_builder.py::_render_skills_index at Hermes v0.22.7.
_INDEX = """You are Hermes.

## Skills
Before replying, scan the skills below. If a skill matches or is even partially relevant to your task, you MUST load it with skill_view(name) and follow its instructions.

<available_skills>
  general:
  - git-commit: Write a conventional commit message
  - weather-brief: Forecast the day
</available_skills>

Only proceed without loading a skill if genuinely none are relevant to the task.
(Categories marked [names only] are outside the current coding context.)

## Memory

User likes tea.
"""

_DELTA = (
    "please commit\n"
    "[System: The skills available in this conversation changed after its system prompt was built.\n"
    "Now available (load with skill_view(name) when relevant, like the skills listed in the system prompt):\n"
    " - secret-skill: does a secret thing]"
)


class StripTests(unittest.TestCase):
    def test_removes_index_and_keeps_following_sections(self) -> None:
        stripped, changed = strip_skills_index(_INDEX)
        self.assertTrue(changed)
        self.assertNotIn("<available_skills>", stripped)
        self.assertNotIn("</available_skills>", stripped)
        self.assertNotIn("git-commit:", stripped)
        self.assertNotIn("weather-brief:", stripped)
        self.assertNotIn("you MUST load it with skill_view", stripped)
        self.assertNotIn("Only proceed without loading a skill", stripped)
        self.assertIn("skills index is withheld", stripped)
        self.assertIn("## Memory", stripped)
        self.assertIn("User likes tea.", stripped)

    def test_unrelated_skills_heading_is_preserved(self) -> None:
        text = "## Skills\n\nI am learning piano skills.\n\n## Memory\n\nTea.\n"
        stripped, changed = strip_skills_index(text)
        self.assertFalse(changed)
        self.assertEqual(stripped, text)

    def test_oneshot_guidance_inside_the_section_is_removed(self) -> None:
        text = (
            "## Skills\n"
            "Scan the skills below and load one with skill_view(name) only when it carries domain knowledge.\n"
            "<available_skills>\n"
            "  - git-commit: Write commits\n"
            "</available_skills>\n"
            "\n## Tools\n\nterminal\n"
        )
        stripped, changed = strip_skills_index(text)
        self.assertTrue(changed)
        self.assertNotIn("git-commit:", stripped)
        self.assertNotIn("load one with skill_view", stripped)
        self.assertIn("## Tools", stripped)
        self.assertIn("terminal", stripped)

    def test_provider_request_is_copied_and_delta_notes_drop(self) -> None:
        request = {
            "system": [{"type": "text", "text": _INDEX}],
            "messages": [
                {"role": "system", "content": _INDEX},
                {"role": "user", "content": _DELTA},
                {"role": "user", "content": "leave this sentence alone"},
            ],
        }
        snapshot = copy.deepcopy(request)
        updated, changed = strip_provider_request(request)
        self.assertTrue(changed)
        self.assertEqual(request, snapshot)
        self.assertNotIn("git-commit:", updated["system"][0]["text"])
        self.assertNotIn("git-commit:", updated["messages"][0]["content"])
        self.assertNotIn("secret-skill", updated["messages"][1]["content"])
        self.assertIn("please commit", updated["messages"][1]["content"])
        self.assertEqual(updated["messages"][2]["content"], "leave this sentence alone")

    def test_clean_request_is_a_noop(self) -> None:
        request = {"messages": [{"role": "system", "content": "You are Hermes.\n\n## Memory\n\nTea.\n"}]}
        updated, changed = strip_provider_request(request)
        self.assertFalse(changed)
        self.assertIs(updated, request)

    def test_middleware_rewrites_only_when_enabled_and_needed(self) -> None:
        store = SkillStore(":memory:")
        self.addCleanup(store.close)
        install_runtime(Runtime(store, settings=Settings(strip_skills_index=True)))
        dirty = {"messages": [{"role": "system", "content": _INDEX}]}
        result = llm_request(request=dirty)
        self.assertIsNotNone(result)
        self.assertEqual(result["source"], "triage")
        self.assertNotIn("<available_skills>", result["request"]["messages"][0]["content"])
        self.assertIn("<available_skills>", dirty["messages"][0]["content"])

        install_runtime(Runtime(store, settings=Settings(strip_skills_index=False)))
        self.assertIsNone(llm_request(request=dirty))

        install_runtime(Runtime(store, settings=Settings(strip_skills_index=True)))
        clean = {"messages": [{"role": "user", "content": "hello"}]}
        self.assertIsNone(llm_request(request=clean))


if __name__ == "__main__":
    unittest.main()
