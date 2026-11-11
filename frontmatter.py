"""SKILL.md frontmatter parser.

Hermes skills are Markdown files with a YAML fence. This parser covers the
subset those files actually use (scalars, inline lists, one-level block lists,
and folded/literal blocks) so the plugin does not depend on PyYAML.
"""

from __future__ import annotations

from typing import Any


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return ``(metadata, body)``. Body is the Markdown after the closing fence."""
    text = text.lstrip("\ufeff")
    if text.startswith("\r\n"):
        normalized = text.replace("\r\n", "\n")
    else:
        normalized = text.replace("\r\n", "\n")
    lines = normalized.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text.strip("\n")
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        return {}, text.strip("\n")
    meta = parse_simple_yaml("\n".join(lines[1:end]))
    body = "\n".join(lines[end + 1 :]).strip("\n")
    return meta, body


def parse_simple_yaml(text: str) -> dict[str, Any]:
    """Parse a flat YAML mapping. Nested mappings are ignored."""
    result: dict[str, Any] = {}
    lines = text.replace("\r\n", "\n").split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or line[:1] in {" ", "\t"}:
            index += 1
            continue
        if ":" not in line:
            index += 1
            continue
        key, _, raw = line.partition(":")
        key = key.strip()
        if not key:
            index += 1
            continue
        raw = raw.strip()
        if raw in {">", ">-", ">+", "|", "|-", "|+"}:
            index += 1
            block: list[str] = []
            while index < len(lines):
                nxt = lines[index]
                if nxt.startswith((" ", "\t")):
                    block.append(nxt.strip())
                    index += 1
                    continue
                if not nxt.strip() and block:
                    break
                if not nxt.strip():
                    index += 1
                    continue
                break
            if raw.startswith(">"):
                result[key] = " ".join(part for part in block if part).strip()
            else:
                result[key] = "\n".join(block).strip()
            continue
        if raw == "" or raw.startswith("#"):
            index += 1
            items: list[str] = []
            while index < len(lines):
                nxt = lines[index]
                if not nxt.strip():
                    index += 1
                    continue
                if nxt[:1] in {" ", "\t"} and nxt.strip().startswith("- "):
                    items.append(_unquote(nxt.strip()[2:].strip()))
                    index += 1
                    continue
                break
            result[key] = items if items else ""
            continue
        if raw.startswith("[") and raw.endswith("]"):
            inner = raw[1:-1].strip()
            if not inner:
                result[key] = []
            else:
                result[key] = [_unquote(part.strip()) for part in inner.split(",") if part.strip()]
            index += 1
            continue
        if " #" in raw:
            raw = raw.split(" #", 1)[0].strip()
        result[key] = _unquote(raw)
        index += 1
    return result


def coerce_tags(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    if "," in text:
        return [part.strip() for part in text.split(",") if part.strip()]
    return [text]


def clip_description(text: str, limit: int = 280) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        inner = value[1:-1]
        if value[0] == '"':
            return inner.replace('\\"', '"').replace("\\n", "\n")
        return inner
    return value
