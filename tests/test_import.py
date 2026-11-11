"""Catalog import, config cut, and the migrate command."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

import support
from triage.config_cut import rewrite_skills_config
from triage.frontmatter import split_frontmatter
from triage.importer import import_tree
from triage.store import SkillStore


class ImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self._tmp_dir())
        self.store = SkillStore(self.tmp / "catalog.db")

    def tearDown(self) -> None:
        self.store.close()

    def _tmp_dir(self) -> str:
        import tempfile

        location = tempfile.mkdtemp(prefix="hsj-import-")
        self.addCleanup(lambda: __import__("shutil").rmtree(location, ignore_errors=True))
        return location

    def test_frontmatter_and_directory_name(self) -> None:
        meta, body = split_frontmatter(
            "---\nname: git-commit\ndescription: \"Write commits\"\ntags: [git, commit]\n---\n\nDo the commit.\n"
        )
        self.assertEqual(meta["name"], "git-commit")
        self.assertEqual(meta["description"], "Write commits")
        self.assertEqual(meta["tags"], ["git", "commit"])
        self.assertIn("Do the commit.", body)

    def test_import_tree_insert_update_and_shadow(self) -> None:
        root = self.tmp / "skills"
        _write_skill(root / "git-commit", "git-commit", "Write a commit message.", "COMMIT_BODY_ONLY")
        _write_skill(root / "alpha", "shared", "First copy.", "FIRST")
        _write_skill(root / "beta", "shared", "Second copy.", "SECOND")
        plain = root / "plain-skill"
        plain.mkdir(parents=True)
        (plain / "SKILL.md").write_text("# Plain\n\nDo the plain thing.\n", encoding="utf-8")

        report = import_tree(self.store, root)
        self.assertIn("git-commit", report.inserted)
        self.assertIn("shared", report.inserted)
        self.assertIn("plain-skill", report.inserted)
        self.assertEqual(len(report.shadowed), 1)
        self.assertNotIn("SECOND", self.store.get("shared").body)

        skill = self.store.get("git-commit")
        self.assertEqual(skill.description, "Write a commit message.")
        self.assertIn("COMMIT_BODY_ONLY", skill.body)
        self.assertEqual(self.store.get("plain-skill").description, "Do the plain thing.")

        again = import_tree(self.store, root)
        self.assertIn("git-commit", again.unchanged)
        self.assertEqual(again.inserted, [])

        _write_skill(root / "git-commit", "git-commit", "Write a commit message.", "COMMIT_BODY_REVISED")
        updated = import_tree(self.store, root)
        self.assertIn("git-commit", updated.updated)
        self.assertIn("COMMIT_BODY_REVISED", self.store.get("git-commit").body)

    def test_demo_tree_and_fts(self) -> None:
        report = import_tree(self.store, support.DEMO_SKILLS)
        self.assertEqual(report.skill_files, 3)
        self.assertEqual(self.store.count(), 3)
        if self.store.fts_enabled:
            names = self.store.fts_names("commit message")
            self.assertIsNotNone(names)
            self.assertIn("git-commit", names)

    def test_rewrite_clears_auto_load_and_external_dirs(self) -> None:
        original = (
            "model: anthropic\n"
            "skills:\n"
            "  auto_load:\n"
            "    - git-commit\n"
            "  external_dirs:\n"
            "    - /tmp/extra\n"
            "  disabled: []\n"
            "memory:\n"
            "  provider: builtin\n"
        )
        result = rewrite_skills_config(original)
        self.assertTrue(result.changed)
        self.assertIn("auto_load: []", result.text)
        self.assertIn("external_dirs: []", result.text)
        self.assertNotIn("git-commit", result.text)
        self.assertNotIn("/tmp/extra", result.text)
        self.assertIn("disabled: []", result.text)
        self.assertIn("provider: builtin", result.text)
        second = rewrite_skills_config(result.text)
        self.assertFalse(second.changed)

    def test_rewrite_appends_when_skills_block_missing(self) -> None:
        result = rewrite_skills_config("model: local\n")
        self.assertTrue(result.changed)
        self.assertIn("skills:\n", result.text)
        self.assertIn("auto_load: []", result.text)
        self.assertIn("external_dirs: []", result.text)

    def test_rewrite_refuses_inline_skills_mapping(self) -> None:
        result = rewrite_skills_config("skills: {auto_load: [git-commit]}\n")
        self.assertFalse(result.changed)
        self.assertIn("inline", result.warning)

    def test_migrate_command_copies_and_can_empty_a_skills_dir(self) -> None:
        db = self.tmp / "catalog.db"
        src = self.tmp / "profile" / "skills"
        _write_skill(src / "git-commit", "git-commit", "Write commits.", "COMMIT_BODY_ONLY")
        config = self.tmp / "profile" / "config.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("skills:\n  auto_load:\n    - git-commit\n", encoding="utf-8")

        base = [sys.executable, str(support.ROOT / "run.py")]
        dry = subprocess.run(
            [*base, "migrate", "--db", str(db), "--from", str(src)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertTrue(src.exists())
        self.assertTrue((src / "git-commit" / "SKILL.md").exists())

        refused = subprocess.run(
            [*base, "migrate", "--db", str(db), "--from", str(src), "--empty-roots"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(refused.returncode, 2)

        applied = subprocess.run(
            [
                *base,
                "migrate",
                "--db",
                str(db),
                "--from",
                str(src),
                "--apply-config",
                "--config",
                str(config),
                "--empty-roots",
                "--yes",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertIn("auto_load: []", config.read_text(encoding="utf-8"))
        self.assertTrue((config.parent / ".no-bundled-skills").exists())
        self.assertEqual(list(src.rglob("SKILL.md")), [])
        self.assertTrue(any(path.name.startswith("skills.bak-triage-") for path in src.parent.iterdir()))

        listed = subprocess.run([*base, "list", "--db", str(db)], capture_output=True, text=True)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("git-commit", listed.stdout)


def _write_skill(directory: Path, name: str, description: str, marker: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{marker}\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
