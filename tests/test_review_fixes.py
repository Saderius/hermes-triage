"""Catalog review fixes (NousResearch/hermes-agent#154048): Hermes' skill guard, confined writes, home folder."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support
import triage.service as service_mod
from triage.importer import import_tree
from triage.service import Runtime, Settings
from triage.store import SkillStore


def _link_dir(link: Path, target: Path) -> bool:
    """A directory link (symlink, or an NTFS junction on Windows). False if neither works here."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        return done.returncode == 0 and link.exists()
    return False


class ReviewFixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="triage-review-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.home = self.tmp / "hermes-home"
        env = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.store = SkillStore(self.tmp / "data" / "catalog.db")
        self.addCleanup(self.store.close)
        self.runtime = Runtime(self.store, settings=Settings())

    def _skill(self, root: Path, name: str) -> Path:
        folder = root / name
        (folder / "references").mkdir(parents=True)
        (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name} skill\n---\n\nBODY_MARKER\n", encoding="utf-8")
        import_tree(self.store, folder, base=root, recursive=False)
        self.runtime.ledger.approve("turn:t", [name], "sess")
        return folder

    def test_hermes_own_skills_go_to_hermes_skill_manage(self) -> None:
        self._skill(self.home / "skills", "hermes-owned")
        self.assertIsNone(self.runtime.serve_skill_manage(
            {"action": "patch", "name": "hermes-owned", "old_string": "BODY_MARKER", "new_string": "X"}, session_id="sess"))

    def test_guard_agent_created_refuses_plugin_served_edits(self) -> None:
        folder = self._skill(self.tmp / "elsewhere", "catalog-skill")
        with mock.patch.object(service_mod, "_hermes_config", return_value={"skills": {"guard_agent_created": True}}):
            for args in (
                {"action": "patch", "name": "catalog-skill", "old_string": "BODY_MARKER", "new_string": "X"},
                {"action": "write_file", "name": "catalog-skill", "file_path": "references/a.md", "file_content": "a"},
                {"operations": [{"action": "edit", "name": "catalog-skill", "old_string": "BODY_MARKER", "new_string": "X"}]},
            ):
                result = json.loads(self.runtime.serve_skill_manage(args, session_id="sess"))
                self.assertFalse(result["success"])
                self.assertIn("guard_agent_created", result["error"])
        self.assertIn("BODY_MARKER", (folder / "SKILL.md").read_text(encoding="utf-8"))
        # With the guard off, the same patch is served.
        with mock.patch.object(service_mod, "_hermes_config", return_value={}):
            result = json.loads(self.runtime.serve_skill_manage(
                {"action": "patch", "name": "catalog-skill", "old_string": "BODY_MARKER", "new_string": "PATCHED"}, session_id="sess"))
        self.assertTrue(result["success"], result)

    def test_writes_and_deletes_stay_inside_the_skill(self) -> None:
        folder = self._skill(self.tmp / "elsewhere", "linked-skill")
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "keep.md").write_text("KEEP", encoding="utf-8")
        shutil.rmtree(folder / "references")
        if not _link_dir(folder / "references", outside):
            self.skipTest("cannot create a directory link here")
        with mock.patch.object(service_mod, "_hermes_config", return_value={}):
            wrote = json.loads(self.runtime.serve_skill_manage(
                {"action": "write_file", "name": "linked-skill", "file_path": "references/evil.md", "file_content": "x"},
                session_id="sess"))
            removed = json.loads(self.runtime.serve_skill_manage(
                {"action": "remove_file", "name": "linked-skill", "file_path": "references/keep.md"}, session_id="sess"))
            patched = json.loads(self.runtime.serve_skill_manage(
                {"action": "patch", "name": "linked-skill", "file_path": "references/keep.md", "old_string": "KEEP", "new_string": "GONE"},
                session_id="sess"))
        for result in (wrote, removed, patched):
            self.assertFalse(result["success"], result)
            self.assertIn("escapes", result["error"])
        self.assertFalse((outside / "evil.md").exists())
        self.assertEqual((outside / "keep.md").read_text(encoding="utf-8"), "KEEP")

    def test_hermes_home_follows_hermes_when_it_is_importable(self) -> None:
        import sys
        import types

        from triage import paths

        fake = types.ModuleType("hermes_constants")
        fake.get_hermes_home = lambda: self.tmp / "profile-home"
        with mock.patch.dict(sys.modules, {"hermes_constants": fake}):
            self.assertEqual(paths.hermes_home(), self.tmp / "profile-home")
        self.assertEqual(paths.hermes_home(), self.home)


if __name__ == "__main__":
    unittest.main()
