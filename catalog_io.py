"""Plugin-owned skill trees and the skill_view / skill_manage payloads.

Hermes ``skill_view`` only searches active skill roots. After ``--empty-roots``
those roots are gone, so an approved call returns "Skill not found" even though
the markdown text is in SQLite. This module copies each skill directory under
the plugin data dir and serves approved reads from that mirror. Writes from
``skill_manage`` land in the same mirror and never in ``~/.hermes/skills``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

from .frontmatter import clip_description, coerce_tags, split_frontmatter
from .store import Skill, SkillStore

# Hermes spills pre_llm_call context above hooks.output_spill.max_chars
# (default 10_000) into a ~1,238-character head/tail stub. Stay under the cap
# so a loaded skill is either inline in full or not inlined at all.
HOOK_CONTEXT_SAFE_CHARS = 8_000
_MAX_FILE_BYTES = 1_048_576
_SUPPORT_DIRS = {"references", "templates", "scripts", "assets"}
SERVED_BY = "triage"


def mirror_root(store: SkillStore) -> Path:
    return store.db_path.parent / "mirrors"


def mirror_dir(store: SkillStore, name: str) -> Path:
    if name.strip(". ") == "" or any(char in name for char in "/\\"):
        raise ValueError(f"unsafe skill name {name!r}")
    return mirror_root(store) / name


def tree_hash(skill_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in _iter_files(skill_dir):
        rel = path.relative_to(skill_dir).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def install_mirror(src_dir: Path, dest_dir: Path) -> int:
    """Copy a skill directory into plugin storage. Returns the file count.

    Symlinks that leave the source tree are skipped. The destination is
    replaced, unless it is the source directory itself.
    """
    src_dir = src_dir.resolve()
    if dest_dir.exists() and dest_dir.resolve() == src_dir:
        return sum(1 for _ in _iter_files(dest_dir))
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for path in _iter_files(src_dir):
        rel = path.relative_to(src_dir)
        target = dest_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
        copied += 1
    return copied


def linked_files(skill_dir: Path | None) -> dict[str, list[str]]:
    """Support files grouped the way Hermes ``skill_view`` reports them."""
    grouped: dict[str, list[str]] = {}
    if skill_dir is None or not skill_dir.is_dir():
        return grouped
    for folder in ("references", "templates", "assets", "scripts"):
        base = skill_dir / folder
        if not base.is_dir():
            continue
        found = [
            path.relative_to(skill_dir).as_posix()
            for path in sorted(base.rglob("*"))
            if path.is_file() and not path.name.startswith(".")
        ]
        if found:
            grouped[folder] = found
    return grouped


def skill_markdown(skill: Skill) -> str:
    """Full SKILL.md text. Prefer the mirror; fall back to the stored body."""
    path = _skill_md(skill)
    if path is not None:
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            pass
    return skill.body


def serve_skill_view(skill: Skill, file_path: str | None = None) -> str:
    """JSON payload in the shape Hermes ``skill_view`` returns to the model."""
    directory = _usable_dir(skill)
    if file_path:
        if directory is None:
            return _json(_fail(
                f"Skill {skill.name!r} has no mirrored files yet, so {file_path!r} cannot be read. "
                "Re-run migrate from the skills backup (do not pass --empty-roots again) so "
                "references, scripts, and assets are copied into plugin storage.",
            ))
        rel = safe_relative(file_path)
        if rel is None:
            return _json(_fail(
                f"file_path {file_path!r} is not a file inside skill {skill.name!r}.",
                hint="Use a relative path such as references/guide.md",
            ))
        target = (directory / rel).resolve()
        try:
            target.relative_to(directory.resolve())
        except ValueError:
            return _json(_fail(f"file_path {file_path!r} escapes skill {skill.name!r}."))
        if not target.is_file():
            available = [item for group in linked_files(directory).values() for item in group]
            return _json(_fail(
                f"File {rel.as_posix()!r} was not found in skill {skill.name!r}.",
                available_files=available,
            ))
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return _json(_fail(f"File {rel.as_posix()!r} is not UTF-8 text."))
        except OSError as exc:
            return _json(_fail(f"Could not read {rel.as_posix()!r}: {exc}"))
        return _json({
            "success": True,
            "name": skill.name,
            "file_path": rel.as_posix(),
            "content": content,
            "skill_dir": str(directory),
            "served_by": SERVED_BY,
        })

    content = skill_markdown(skill)
    linked = linked_files(directory)
    payload = {
        "success": True,
        "name": skill.name,
        "description": skill.description,
        "tags": list(skill.tags),
        "content": content,
        "skill_dir": str(directory) if directory else None,
        "linked_files": linked or None,
        "served_by": SERVED_BY,
    }
    if linked:
        payload["usage_hint"] = (
            "To view linked files, call skill_view(name, file_path) where file_path "
            "is e.g. 'references/api.md' or 'scripts/run.py'."
        )
    return _json(payload)


def apply_skill_manage(store: SkillStore, args: dict) -> dict:
    """Apply one skill_manage call to the plugin mirror. Never touches Hermes roots."""
    operations = args.get("operations")
    if isinstance(operations, list):
        results = []
        for index, op in enumerate(operations):
            if not isinstance(op, dict):
                return _fail(f"operations[{index}] is not an object.")
            merged = dict(op)
            if not merged.get("name") and args.get("name"):
                merged["name"] = args["name"]
            result = _one_manage(store, merged)
            results.append(result)
            if not result.get("success"):
                return {
                    "success": False,
                    "error": result.get("error") or "skill_manage failed",
                    "completed_before_failure": index,
                    "results": results,
                    "served_by": SERVED_BY,
                }
        return {"success": True, "results": results, "served_by": SERVED_BY}
    return _one_manage(store, args)


def activation_text(skill: Skill, *, probability: float | None, why: str, full: str) -> str:
    """User-message context for an approved skill.

    Bodies that would cross Hermes' hook-output spill limit are not inlined.
    The model is told to load them with ``skill_view``, which this plugin serves
    in full as a tool result.
    """
    if len(full) <= HOOK_CONTEXT_SAFE_CHARS:
        return full
    pct = ""
    if probability:
        from .judge import format_percent

        pct = f" ({format_percent(probability)})"
    reason = f" — {why}" if why else ""
    directory = _usable_dir(skill)
    linked = linked_files(directory)
    flat = [item for group in linked.values() for item in group]
    lines = [
        f"[triage] Approved skill {skill.name}{pct}{reason}.",
        "The full instructions are not in this message. Hermes truncates plugin "
        "hook context above 10000 characters, so the body is served by skill_view.",
        f"Call skill_view(name={skill.name!r}) and follow that tool result. "
        "It is the complete skill.",
        skill.description,
    ]
    if flat:
        shown = ", ".join(flat[:12])
        lines.append(f"Linked files: {shown}")
        lines.append(
            "Read one with skill_view(name=%r, file_path='references/example.md')." % skill.name
        )
    return "\n".join(lines)


def _one_manage(store: SkillStore, args: dict) -> dict:
    action = str(args.get("action") or "").strip().lower()
    name = str(args.get("name") or "").strip()
    if action in {"edit"}:
        action = "patch"
    if not name:
        return _fail("name is required.")
    if any(char in name for char in "/\\\n\r\t") or len(name) > 80:
        return _fail(f"invalid skill name {name!r}.")
    if action == "create":
        return _create(store, name, args.get("content"))
    existing = store.get(name)
    if existing is None:
        return _fail(f"Skill {name!r} is not in the plugin catalog. Create it with action='create'.")
    if action == "delete":
        return _delete(store, existing)
    if action == "write_file":
        return _write_support(store, existing, args.get("file_path"), _file_text(args))
    if action == "remove_file":
        return _remove_support(store, existing, args.get("file_path"))
    if action == "patch":
        content = args.get("content")
        if isinstance(content, str) and content.strip() and not args.get("old_string"):
            return _rewrite_skill_md(store, existing, content)
        return _patch(store, existing, args)
    return _fail(
        f"Unknown action {action!r}. Use create, patch, delete, write_file, or remove_file."
    )


def _create(store: SkillStore, name: str, content: object) -> dict:
    if store.get(name) is not None:
        return _fail(f"A skill named {name!r} already exists in the plugin catalog.")
    if not isinstance(content, str) or not content.strip():
        return _fail("content is required for create. Provide the full SKILL.md text.")
    if not isinstance(name, str) or name.strip(". ") == "" or any(char in name for char in "/\\"):
        return _fail(f"{name!r} is not a usable skill name.")
    directory = mirror_dir(store, name)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(content, encoding="utf-8")
    _sync_row(store, name, directory, content, origin_path="SKILL.md")
    return {
        "success": True,
        "message": f"Skill {name!r} created in the plugin catalog.",
        "path": str(directory),
        "skill_md": str(directory / "SKILL.md"),
        "served_by": SERVED_BY,
        "hint": (
            f"skill_view(name={name!r}) reads it back. "
            "Supporting files: skill_manage(action='write_file', "
            f"name={name!r}, file_path='references/example.md', file_content='...')."
        ),
    }


def _confined(directory: Path, rel: Path) -> Path | None:
    """``directory / rel`` with links resolved, or None when it lands outside ``directory``.

    A symlinked subfolder (``references`` pointing elsewhere) would otherwise
    redirect a write or a delete out of the skill."""
    target = (directory / rel).resolve()
    try:
        target.relative_to(directory.resolve())
    except ValueError:
        return None
    return target


def _rewrite_skill_md(store: SkillStore, skill: Skill, content: str) -> dict:
    directory = _ensure_dir(store, skill)
    (directory / "SKILL.md").write_text(content, encoding="utf-8")
    _sync_row(store, skill.name, directory, content, origin_path=skill.origin_path or "SKILL.md")
    return {
        "success": True,
        "message": f"Skill {skill.name!r} updated (full rewrite) in the plugin catalog.",
        "path": str(directory),
        "served_by": SERVED_BY,
    }


def _patch(store: SkillStore, skill: Skill, args: dict) -> dict:
    old = args.get("old_string")
    new = args.get("new_string")
    if not isinstance(old, str) or old == "":
        return _fail("patch requires old_string, or content alone for a full SKILL.md rewrite.")
    if not isinstance(new, str):
        return _fail("patch requires new_string.")
    rel = safe_relative(str(args.get("file_path") or "SKILL.md"))
    if rel is None:
        return _fail("file_path escapes the skill directory.")
    directory = _ensure_dir(store, skill)
    target = _confined(directory, rel)
    if target is None:
        return _fail(f"file_path {rel.as_posix()!r} escapes skill {skill.name!r}.")
    if not target.is_file():
        return _fail(f"File {rel.as_posix()!r} was not found in skill {skill.name!r}.")
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0:
        return _fail(f"old_string was not found in {rel.as_posix()}.")
    replace_all = bool(args.get("replace_all"))
    if count > 1 and not replace_all:
        return _fail(
            f"old_string matched {count} times in {rel.as_posix()}. Pass replace_all=true or a longer old_string."
        )
    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    target.write_text(updated, encoding="utf-8")
    if rel.as_posix() == "SKILL.md":
        _sync_row(store, skill.name, directory, updated, origin_path=skill.origin_path or "SKILL.md")
    return {
        "success": True,
        "message": f"Patched {rel.as_posix()} in skill {skill.name!r}.",
        "path": str(target),
        "served_by": SERVED_BY,
    }


def _write_support(store: SkillStore, skill: Skill, file_path: object, file_content: object) -> dict:
    rel = safe_relative(str(file_path or ""))
    if rel is None or not rel.parts or rel.parts[0] not in _SUPPORT_DIRS:
        return _fail(
            "write_file requires file_path under references/, templates/, scripts/, or assets/."
        )
    if not isinstance(file_content, str):
        return _fail("file_content is required for write_file.")
    if len(file_content.encode("utf-8")) > _MAX_FILE_BYTES:
        return _fail("file_content exceeds 1 MiB.")
    directory = _ensure_dir(store, skill)
    target = _confined(directory, rel)
    if target is None:
        return _fail(f"file_path {rel.as_posix()!r} escapes skill {skill.name!r}.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(file_content, encoding="utf-8")
    return {
        "success": True,
        "message": f"File {rel.as_posix()!r} written to skill {skill.name!r}.",
        "path": str(target),
        "served_by": SERVED_BY,
    }


def _remove_support(store: SkillStore, skill: Skill, file_path: object) -> dict:
    rel = safe_relative(str(file_path or ""))
    if rel is None or not rel.parts or rel.parts[0] not in _SUPPORT_DIRS:
        return _fail("remove_file requires file_path under references/, templates/, scripts/, or assets/.")
    directory = _usable_dir(skill)
    if directory is None:
        return _fail(f"Skill {skill.name!r} has no mirrored directory.")
    target = _confined(directory, rel)
    if target is None:
        return _fail(f"file_path {rel.as_posix()!r} escapes skill {skill.name!r}.")
    if not target.is_file():
        return _fail(f"File {rel.as_posix()!r} was not found in skill {skill.name!r}.")
    target.unlink()
    return {
        "success": True,
        "message": f"File {rel.as_posix()!r} removed from skill {skill.name!r}.",
        "served_by": SERVED_BY,
    }


def _delete(store: SkillStore, skill: Skill) -> dict:
    directory = _usable_dir(skill)
    if directory is not None and not _inside(directory, mirror_root(store)):
        # Indexed in place (a bridge): the folder is the user's, not Triage's.
        return _fail(
            f"Skill {skill.name!r} lives in {directory}, which Triage only indexes. "
            "It was not deleted; remove that folder yourself if you want it gone."
        )
    if directory is not None and directory.is_dir():
        shutil.rmtree(directory)
    store.delete(skill.name)
    return {
        "success": True,
        "message": f"Skill {skill.name!r} deleted from the plugin catalog.",
        "served_by": SERVED_BY,
    }


def _sync_row(store: SkillStore, name: str, directory: Path, skill_md_text: str, *, origin_path: str) -> None:
    meta, body = split_frontmatter(skill_md_text)
    if not body.strip():
        body = skill_md_text.strip()
    description = clip_description(str(meta.get("description") or ""))
    if not description:
        description = clip_description(name)
    tags = coerce_tags(meta.get("tags"))
    store.upsert(
        name=name,
        description=description,
        body=body,
        tags=tags,
        source=str(directory),
        origin_path=origin_path,
        content_hash=tree_hash(directory),
    )


def _ensure_dir(store: SkillStore, skill: Skill) -> Path:
    directory = _usable_dir(skill)
    if directory is not None:
        return directory
    directory = mirror_dir(store, skill.name)
    directory.mkdir(parents=True, exist_ok=True)
    text = skill.body if skill.body.lstrip().startswith("---") else (
        f"---\nname: {skill.name}\ndescription: {skill.description}\n---\n\n{skill.body}\n"
    )
    (directory / "SKILL.md").write_text(text, encoding="utf-8")
    return directory


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _usable_dir(skill: Skill) -> Path | None:
    if not skill.source:
        return None
    directory = Path(skill.source)
    if directory.is_dir() and (directory / "SKILL.md").is_file():
        return directory
    return None


def _skill_md(skill: Skill) -> Path | None:
    directory = _usable_dir(skill)
    if directory is None:
        return None
    path = directory / "SKILL.md"
    return path if path.is_file() else None


def safe_relative(file_path: str) -> Path | None:
    if not isinstance(file_path, str):
        return None
    raw = file_path.replace("\\", "/").strip()
    if not raw or raw.startswith("/") or raw.startswith("~"):
        return None
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts:
        return None
    return path


def _file_text(args: dict) -> object:
    if args.get("file_content") is not None:
        return args.get("file_content")
    return args.get("content")


def _iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            name for name in dirnames if not name.startswith(".") and name != "__pycache__"
        )
        for filename in sorted(filenames):
            if filename.startswith("."):
                continue
            path = Path(dirpath) / filename
            if path.is_symlink():
                try:
                    resolved = path.resolve()
                    resolved.relative_to(root.resolve())
                except (OSError, ValueError):
                    continue
                path = resolved
            try:
                if not path.is_file() or path.stat().st_size > _MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield path


def _fail(error: str, **extra) -> dict:
    payload = {"success": False, "error": error, "served_by": SERVED_BY}
    payload.update(extra)
    return payload


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)
