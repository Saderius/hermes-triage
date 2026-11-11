"""Tests for history_loads (skills loaded outside Triage)."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import support
from triage.context import history_loads


class HistoryLoadsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="hsj-history-loads-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.state = self.tmp / "state.db"

        conn = sqlite3.connect(self.state)
        conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, tool_name TEXT, "
            "content TEXT, active INTEGER, timestamp REAL)"
        )
        rows = [
            ("s1", "user", None, '[IMPORTANT: The user has invoked the "firebase" skill, indicating they want you to follow it.] body', 1, 100.0),
            ("s1", "user", None, '[IMPORTANT: The user has invoked the "/ux-audit /android-dev" stacked skill bundle, loading 2 skills together.] Skills loaded: ux-audit, android-dev [Loaded as part of the stacked skill invocation "ux-audit".] body', 0, 200.0),
            ("s1", "tool", "skill_view", '{"success": true, "name": "git-commit", "description": "x"}', 1, 300.0),
            ("s1", "tool", "skill_view", '{"success": false, "error": "blocked", "name": "pr-review"}', 1, 400.0),
            ("s1", "tool", "skill_load", '{"loaded": [{"name": "triage:weather-brief", "instructions": "x"}]}', 0, 500.0),
            ("s1", "user", None, "an ordinary message that mentions the firebase skill", 1, 600.0),
            ("s2", "tool", "skill_view", '{"success": true, "name": "other-chat-skill"}', 1, 700.0),
        ]
        conn.executemany("INSERT INTO messages (session_id, role, tool_name, content, active, timestamp) VALUES (?, ?, ?, ?, ?, ?)", rows)
        conn.commit()
        conn.close()

    def test_history_loads_order_and_state(self) -> None:
        loads = history_loads(["s1"], db_path=self.state)
        pairs = [(item["name"], item["state"]) for item in loads]
        self.assertEqual(
            pairs,
            [
                ("firebase", "in"),
                ("ux-audit", "released"),
                ("android-dev", "released"),
                ("git-commit", "in"),
                ("weather-brief", "released"),
            ],
        )
        self.assertEqual(loads[0]["at"], "1970-01-01T00:01:40+00:00")

    def test_history_loads_empty_and_missing(self) -> None:
        self.assertEqual(history_loads([], db_path=self.state), [])
        self.assertEqual(history_loads(["s1"], db_path=self.tmp / "missing.db"), [])
        self.assertEqual(history_loads(["nobody"], db_path=self.state), [])

    def test_reloaded_skill_becomes_in(self) -> None:
        first = history_loads(["s1"], db_path=self.state)
        dev = next(item for item in first if item["name"] == "android-dev")
        self.assertEqual(dev["state"], "released")

        conn = sqlite3.connect(self.state)
        conn.execute(
            "INSERT INTO messages (session_id, role, tool_name, content, active, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            ("s1", "tool", "skill_view", '{"success": true, "name": "android-dev"}', 1, 800.0),
        )
        conn.commit()
        conn.close()

        second = history_loads(["s1"], db_path=self.state)
        dev_after = next(item for item in second if item["name"] == "android-dev")
        self.assertEqual(dev_after["state"], "in")

    def test_multiple_sessions(self) -> None:
        loads = history_loads(["s1", "s2"], db_path=self.state)
        names = [item["name"] for item in loads]
        self.assertIn("other-chat-skill", names)

    def test_pruned_result_is_released_even_in_an_active_message(self) -> None:
        # Hermes keeps the tool message after a compaction but replaces its text.
        conn = sqlite3.connect(self.state)
        conn.execute(
            "INSERT INTO messages (session_id, role, tool_name, content, active, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            ("s3", "tool", "skill_view",
             "[skill_view] name=autonomous-ai-agents\\hermes-agent (5,617 chars) [SKILL_PRUNED: content lost in compression]",
             1, 900.0),
        )
        conn.commit()
        conn.close()
        loads = history_loads(["s3"], db_path=self.state)
        self.assertEqual([(item["name"], item["state"]) for item in loads], [("hermes-agent", "released")])


if __name__ == "__main__":
    unittest.main()
