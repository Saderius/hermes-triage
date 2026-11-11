"""Walk a SKILL.md tree and index it in the plugin store.

Import is a bridge, not a copy: the catalog keeps the skill's text for search
and the path of its folder, and serves files from that folder where it lives.
Nothing is copied, moved or deleted, and Hermes is not edited.

The one exception is ``migrate`` (``copy=True``): it exists to move skills out
of Hermes' own folders, so it keeps a copy in plugin storage first.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .catalog_io import install_mirror, mirror_dir, tree_hash
from .frontmatter import clip_description, coerce_tags, split_frontmatter
from .store import SkillStore


@dataclass
class ImportReport:
    root: str
    inserted: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    shadowed: list[str] = field(default_factory=list)

    @property
    def skill_files(self) -> int:
        return len(self.inserted) + len(self.updated) + len(self.unchanged) + len(self.shadowed)

    def summary(self) -> str:
        return (
            f"imported {len(self.inserted)} new, "
            f"updated {len(self.updated)}, "
            f"unchanged {len(self.unchanged)}, "
            f"shadowed {len(self.shadowed)}, "
            f"skipped {len(self.skipped)} "
            f"from {self.root}"
        )


def import_tree(
    store: SkillStore,
    root: Path,
    *,
    source_label: str | None = None,
    base: Path | None = None,
    recursive: bool = True,
    copy: bool = False,
) -> ImportReport:
    """Import every SKILL.md under ``root`` (only ``root/SKILL.md`` when not ``recursive``).

    ``origin_path`` is recorded relative to ``base`` (default ``root``)."""
    root = Path(root)
    report = ImportReport(root=str(root))
    if not root.exists() or not root.is_dir():
        report.skipped.append(f"missing directory: {root}")
        return report
    resolved_root = root.resolve()
    relative_base = Path(base).resolve() if base is not None else resolved_root
    del source_label  # provenance lives in the report root; `skills.source` is the skill folder
    seen: dict[str, str] = {}
    files = _iter_skill_files(resolved_root) if recursive else (
        [resolved_root / "SKILL.md"] if (resolved_root / "SKILL.md").is_file() else []
    )
    for skill_path in files:
        try:
            text = skill_path.read_text(encoding="utf-8")
        except OSError as exc:
            report.skipped.append(f"{skill_path}: {exc}")
            continue
        meta, body = split_frontmatter(text)
        name = str(meta.get("name") or skill_path.parent.name).strip()
        if not _valid_name(name):
            report.skipped.append(f"{skill_path}: invalid name {name!r}")
            continue
        description = clip_description(str(meta.get("description") or ""))
        if not description:
            description = clip_description(_first_paragraph(body) or name)
        tags = coerce_tags(meta.get("tags"))
        if not body.strip():
            body = text.strip()
        key = name.casefold()
        if key in seen:
            report.shadowed.append(f"{name} from {skill_path} (kept {seen[key]})")
            continue
        seen[key] = str(skill_path)
        try:
            relative = str(skill_path.relative_to(relative_base))
        except ValueError:
            report.skipped.append(f"{skill_path}: outside import root")
            continue
        home = mirror_dir(store, name) if copy else skill_path.parent
        try:
            digest = tree_hash(skill_path.parent)
        except OSError as exc:
            report.skipped.append(f"{skill_path}: {exc}")
            continue
        outcome = store.upsert(
            name=name,
            description=description,
            body=body,
            tags=tags,
            source=str(home),
            origin_path=relative,
            content_hash=digest,
            refs=collect_reference_text(skill_path.parent),
        )
        if copy and (outcome != "unchanged" or not (home / "SKILL.md").is_file()):
            try:
                install_mirror(skill_path.parent, home)
            except OSError as exc:
                report.skipped.append(f"{skill_path}: copy failed: {exc}")
                continue
        getattr(report, outcome).append(name)
    return report


def _iter_skill_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not name.startswith(".") and "bak-skill-judge" not in name and "bak-triage" not in name
        )
        if "SKILL.md" not in filenames:
            continue
        candidate = Path(dirpath) / "SKILL.md"
        if candidate.is_symlink():
            try:
                target = candidate.resolve()
            except OSError:
                continue
            try:
                target.relative_to(root)
            except ValueError:
                continue
        yield candidate


def collect_reference_text(skill_dir: Path, limit: int = 200_000) -> str:
    """Text of mirrored references and templates, for the full-body index.

    The cap is large on purpose. A sentence near the end of a reference still
    has to be searchable. Binary files are skipped.
    """
    chunks: list[str] = []
    total = 0
    for folder in ("references", "templates"):
        base = skill_dir / folder
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in {".md", ".txt", ".rst"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            chunks.append(text)
            total += len(text)
            if total >= limit:
                return "\n".join(chunks)[:limit]
    return "\n".join(chunks)


def _valid_name(name: str) -> bool:
    if not name or len(name) > 80:
        return False
    if name.strip(". ") == "":
        return False
    if any(char in name for char in "/\\\n\r\t"):
        return False
    return True


def _first_paragraph(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""
