"""Print the post-compaction unlocked-skills stub for a fake session.

    python3 scripts/post_compact_stub.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _install_package() -> None:
    import types

    name = "triage"
    if name in sys.modules and getattr(sys.modules[name], "__path__", None):
        return
    package = types.ModuleType(name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    package.__package__ = name
    sys.modules[name] = package


_install_package()
from triage.importer import import_tree
from triage.service import Runtime
from triage.store import SkillStore


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="hsj-stub-"))
    store = SkillStore(tmp / "catalog.db")
    import_tree(store, ROOT / "examples" / "demo-skills")
    store.upsert(
        name="android-debug",
        description="Debug Android Kotlin apps with Logcat.",
        body="ANDROID_ONLY_BODY",
        tags=["android"],
        source="",
        origin_path="android-debug/SKILL.md",
        content_hash="android",
        refs="",
    )
    runtime = Runtime(store)
    runtime.ledger.approve("turn:demo", ["android-debug", "git-commit"], "demo-session")
    runtime.on_post_compaction(session_id="demo-session", kind="summarize", reason="manual")
    print(runtime.unlocked_stub("demo-session"))
    store.close()


if __name__ == "__main__":
    main()
