"""Tests for the ranking evaluation set (evalset)."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import support
from triage.evalset import Case, collect, evaluate, format_report, load_cases, snapshot
from triage.importer import import_tree
from triage.store import SkillStore

DEMO = support.ROOT / "examples" / "eval-demo.jsonl"


class EvalSetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="triage-test-evalset-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.db_path = self.tmp / "catalog.db"
        self.store = SkillStore(self.db_path)
        self.addCleanup(self.store.close)
        import_tree(self.store, support.DEMO_SKILLS)

    def test_load_cases(self) -> None:
        cases_file = self.tmp / "sample_cases.jsonl"
        cases_file.write_text(
            "# A comment line\n"
            "\n"
            '{"draft": "sample draft", "expect": ["git-commit"], "kind": "named"}\n'
            '{"draft": "second draft", "expect": null}\n'
            '{"draft": "third draft"}\n',
            encoding="utf-8",
        )
        cases = load_cases(cases_file)
        self.assertEqual(len(cases), 3)
        self.assertEqual(cases[0].draft, "sample draft")
        self.assertEqual(cases[0].expect, ("git-commit",))
        self.assertEqual(cases[0].kind, "named")
        self.assertIsNone(cases[1].expect)
        self.assertIsNone(cases[2].expect)

        bad_file = self.tmp / "bad_cases.jsonl"
        bad_file.write_text("not json\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_cases(bad_file)

    def test_evaluate(self) -> None:
        cases = load_cases(DEMO)
        summary = evaluate(self.store, cases)
        self.assertEqual(summary["cases"], 10)
        self.assertEqual(summary["unlabelled"], 0)
        self.assertEqual(summary["wanted"], 7)
        self.assertEqual(summary["none_cases"], 3)
        self.assertEqual(summary["none_auto"], 0)
        self.assertEqual(summary["by_kind"]["named"], {"wanted": 1, "top1": 1, "topk": 1})
        self.assertEqual(summary["by_kind"]["described"], {"wanted": 3, "top1": 3, "topk": 3})
        self.assertEqual(summary["by_kind"]["paraphrase"]["wanted"], 3)
        self.assertEqual(summary["auto_right"], summary["auto_fired"])
        self.assertGreaterEqual(summary["auto_fired"], 3)
        self.assertEqual(len(summary["misses"]), summary["wanted"] - summary["topk"])

    def test_sweep(self) -> None:
        cases = load_cases(DEMO)
        summary = evaluate(self.store, cases)
        sweep = summary["sweep"]
        fired_counts = [entry["auto_fired"] for entry in sweep]
        for a, b in zip(fired_counts, fired_counts[1:]):
            self.assertGreaterEqual(a, b)
        at_90 = [entry for entry in sweep if abs(entry["threshold"] - 0.90) < 1e-6][0]
        self.assertEqual(at_90["auto_fired"], summary["auto_fired"])

    def test_unlabelled_cases(self) -> None:
        cases = [
            Case("draft with expect", ("git-commit",), "named"),
            Case("draft without expect", None, ""),
        ]
        summary = evaluate(self.store, cases)
        self.assertEqual(summary["cases"], 1)
        self.assertEqual(summary["unlabelled"], 1)

    def test_format_report(self) -> None:
        cases = load_cases(DEMO)
        summary = evaluate(self.store, cases)
        report = format_report(summary)
        self.assertIn("7", report)

        empty_summary = evaluate(self.store, [])
        empty_report = format_report(empty_summary)
        self.assertIn("n/a", empty_report)

    def test_snapshot(self) -> None:
        orig_stat = self.db_path.stat()
        orig_size = orig_stat.st_size
        orig_mtime = orig_stat.st_mtime
        orig_count = self.store.count()

        dest_dir = self.tmp / "snap"
        snap_path = snapshot(self.db_path, dest_dir)
        snap_store = SkillStore(snap_path)
        try:
            self.assertEqual(snap_store.count(), orig_count)
        finally:
            snap_store.close()

        new_stat = self.db_path.stat()
        self.assertEqual(new_stat.st_size, orig_size)
        self.assertEqual(new_stat.st_mtime, orig_mtime)

    def test_collect(self) -> None:
        state_db = self.tmp / "state.db"
        conn = sqlite3.connect(state_db)
        try:
            conn.execute(
                "CREATE TABLE messages ("
                "id INTEGER PRIMARY KEY, "
                "session_id TEXT, "
                "role TEXT, "
                "content TEXT, "
                "api_content TEXT, "
                "active INTEGER"
                ")"
            )
            # Two user rows (one of them twice)
            conn.execute(
                "INSERT INTO messages (id, session_id, role, content, api_content, active) "
                "VALUES (1, 's1', 'user', 'commit these staged changes', NULL, 1)"
            )
            conn.execute(
                "INSERT INTO messages (id, session_id, role, content, api_content, active) "
                "VALUES (2, 's1', 'user', 'commit these staged changes', NULL, 1)"
            )
            conn.execute(
                "INSERT INTO messages (id, session_id, role, content, api_content, active) "
                "VALUES (3, 's1', 'user', 'check weather in Lisbon today', NULL, 1)"
            )
            # Assistant row
            conn.execute(
                "INSERT INTO messages (id, session_id, role, content, api_content, active) "
                "VALUES (4, 's1', 'assistant', 'sure I can do that', NULL, 1)"
            )
            # Inactive user row
            conn.execute(
                "INSERT INTO messages (id, session_id, role, content, api_content, active) "
                "VALUES (5, 's1', 'user', 'inactive message content here', NULL, 0)"
            )
            # Two-word user row
            conn.execute(
                "INSERT INTO messages (id, session_id, role, content, api_content, active) "
                "VALUES (6, 's1', 'user', 'two words', NULL, 1)"
            )
            conn.commit()
        finally:
            conn.close()

        out_path = self.tmp / "eval" / "drafts.jsonl"
        added = collect(state_db, out_path, store=self.store)
        self.assertEqual(added, 2)

        lines = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)
        self.assertIsNone(lines[0]["expect"])
        self.assertIsNone(lines[1]["expect"])

        # Label the first line by hand in the test
        lines[0]["expect"] = ["git-commit"]
        out_path.write_text("\n".join(json.dumps(item) for item in lines) + "\n", encoding="utf-8")

        # Call collect again
        added_again = collect(state_db, out_path, store=self.store)
        self.assertEqual(added_again, 0)

        lines_again = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(lines_again[0]["expect"], ["git-commit"])

        # A draft tidied by hand keeps its collected text in "raw" and is not collected again.
        tidied = {"draft": "commit the staged changes", "expect": ["git-commit"], "raw": "commit these staged changes"}
        other = next(item for item in lines if item["draft"] != tidied["raw"])
        out_path.write_text(json.dumps(tidied) + "\n" + json.dumps(other) + "\n", encoding="utf-8")
        self.assertEqual(collect(state_db, out_path), 0)

        # A hand-edited file without a final newline keeps its last case intact.
        out_path.write_text('{"draft": "an older labelled draft", "expect": []}', encoding="utf-8")
        self.assertEqual(collect(state_db, out_path), 2)
        kept = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(kept), 3)
        self.assertEqual(kept[0], {"draft": "an older labelled draft", "expect": []})
        self.assertEqual(kept[1]["hint"], [])

    def test_collect_takes_the_typed_text(self) -> None:
        state_db = self.tmp / "state.db"
        conn = sqlite3.connect(state_db)
        try:
            conn.execute(
                "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
                "content TEXT, api_content TEXT, active INTEGER)"
            )
            conn.execute(
                "INSERT INTO messages VALUES (1, 's1', 'user', 'review this pull request', "
                "'review this pull request ## Router notes for the model', 1)"
            )
            conn.commit()
        finally:
            conn.close()
        out_path = self.tmp / "typed.jsonl"
        self.assertEqual(collect(state_db, out_path), 1)
        self.assertEqual(json.loads(out_path.read_text(encoding="utf-8"))["draft"], "review this pull request")


if __name__ == "__main__":
    unittest.main()
