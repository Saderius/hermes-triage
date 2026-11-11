"""Tests for auto-attach evidence calibration."""

from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest

import support
from triage.context import auto_evidence, chip_ranking, prose_only
from triage.importer import import_tree
from triage.judge import SkillJudge
from triage.store import SkillStore


class AutoEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="hsj-auto-evidence-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

        self.demo = SkillStore(self.tmp / "demo.db")
        self.addCleanup(self.demo.close)
        import_tree(self.demo, support.DEMO_SKILLS)

        self.big = SkillStore(self.tmp / "big.db")
        self.addCleanup(self.big.close)

        for i in range(210):
            name = f"filler-{i:03d}"
            self.big.upsert(
                name=name,
                description=f"Generic helper number {i}.",
                body="We go and build things on a base with issues.",
                tags=[],
                source=str(self.tmp),
                origin_path=f"{name}/SKILL.md",
                content_hash=f"hash-{name}",
                refs="",
            )

        self.big.upsert(
            name="go",
            description="Go language guidelines for services.",
            body="Use go modules.",
            tags=[],
            source=str(self.tmp),
            origin_path="go/SKILL.md",
            content_hash="hash-go",
            refs="",
        )
        self.big.upsert(
            name="firebase",
            description="Firebase backend: auth, database, hosting.",
            body="Firebase rules.",
            tags=[],
            source=str(self.tmp),
            origin_path="firebase/SKILL.md",
            content_hash="hash-firebase",
            refs="",
        )
        self.big.upsert(
            name="android-cli",
            description="Install and use the android command-line tool.",
            body="Run android.",
            tags=[],
            source=str(self.tmp),
            origin_path="android-cli/SKILL.md",
            content_hash="hash-android-cli",
            refs="",
        )

    def test_doc_frequency(self) -> None:
        self.assertEqual(self.big.doc_frequency("firebase"), 1)
        self.assertGreaterEqual(self.big.doc_frequency("go"), 210)
        self.assertEqual(self.big.doc_frequency(""), self.big.count())

    def test_auto_evidence_common_word(self) -> None:
        self.assertFalse(auto_evidence(self.big, "let's go with the second option", self.big.get("go")))

    def test_auto_evidence_rare_word(self) -> None:
        self.assertTrue(auto_evidence(self.big, "do you have access to firebase here?", self.big.get("firebase")))

    def test_auto_evidence_two_word_name(self) -> None:
        self.assertTrue(auto_evidence(self.big, "use android-cli to install the apk", self.big.get("android-cli")))

    def test_auto_evidence_not_prose(self) -> None:
        skill = self.big.get("firebase")
        self.assertFalse(auto_evidence(self.big, r"open D:\projects\demo\firebase\notes.txt please", skill))
        self.assertFalse(auto_evidence(self.big, "look at `firebase` here", skill))
        self.assertFalse(auto_evidence(self.big, r"@folder:`C:\x\firebase` what is this", skill))
        self.assertFalse(auto_evidence(self.big, "see https://example.com/firebase now", skill))

    def test_prose_only(self) -> None:
        self.assertNotIn("firebase", prose_only("a ```firebase\nrules``` b"))

    def test_small_catalog(self) -> None:
        skill = self.demo.get("git-commit")
        self.assertTrue(auto_evidence(self.demo, "review the commit message", skill))
        self.assertFalse(auto_evidence(self.demo, "hello there", skill))

    def test_chip_ranking(self) -> None:
        candidates_go = chip_ranking(SkillJudge(self.big), "let's go with the second option", "", limit=5)["candidates"]
        go_row = next((r for r in candidates_go if r["name"] == "go"), None)
        self.assertIsNotNone(go_row)
        self.assertFalse(go_row["anchored"])

        candidates_fb = chip_ranking(SkillJudge(self.big), "do you have access to firebase here?", "", limit=5)["candidates"]
        fb_row = next((r for r in candidates_fb if r["name"] == "firebase"), None)
        self.assertIsNotNone(fb_row)
        self.assertTrue(fb_row["anchored"])


if __name__ == "__main__":
    unittest.main()
