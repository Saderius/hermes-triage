"""Strip the Hermes skills index from an outbound provider request.

Hermes v0.22.7 has no plugin API that removes ``build_skills_system_prompt``.
The index is rendered as a ``## Skills`` section containing
``<available_skills>…</available_skills>`` (see ``agent/prompt_builder.py``).
This middleware deletes that block from system messages and replaces it with
a constant two-line stub so later turns can share a stable prefix again.

Rewriting the system message breaks the provider prefix cache for the turn
where the unstripped prompt and the stripped prompt differ. Once the stub is
stable, later turns match each other. If the index is already absent the
middleware returns ``None`` and does not touch the request.
"""

from __future__ import annotations

import re
from typing import Any

_AVAILABLE = re.compile(
    r"<available_skills\b[^>]*>.*?</available_skills>",
    re.IGNORECASE | re.DOTALL,
)
_SKILLS_HEADING = re.compile(r"(?m)^## Skills[ \t]*$")
_OTHER_HEADING = re.compile(r"(?m)^#{1,6} ")
_DELTA_NOTE = re.compile(
    r"\[System: The skills available in this conversation changed after its system prompt was built\..*?\]",
    re.DOTALL,
)

# Lines that follow </available_skills> in the v0.22.7 renderer.
_EPILOGUE_PREFIXES = (
    "only proceed without loading a skill",
    "(categories marked",
    "(a copy of ",
)

_STUB = (
    "## Skills\n"
    "\n"
    "The skills index is withheld. triage supplies at most one "
    "skill body in the user message after a probability shortlist. Do not "
    "call skill_view unless that message names an approved skill.\n"
)


def strip_skills_index(text: str) -> tuple[str, bool]:
    """Remove skills-index blocks from one string. Returns ``(text, changed)``."""
    if not text:
        return text, False
    original = text
    text = _DELTA_NOTE.sub("", text)
    guard = 0
    while _AVAILABLE.search(text) and guard < 8:
        guard += 1
        match = _AVAILABLE.search(text)
        if match is None:
            break
        start = _expand_start(text, match.start())
        end = _expand_end(text, match.end())
        text = text[:start] + _STUB + text[end:]
    if text == original:
        return original, False
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text, True


def strip_provider_request(request: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return a shallow-copied request with system skills indexes removed.

    The input mapping is not mutated.
    """
    if not isinstance(request, dict):
        return request, False
    changed = False
    out = dict(request)
    for key in ("system", "instructions"):
        if key not in out:
            continue
        new_value, did = _strip_content(out[key], system=True)
        if did:
            out[key] = new_value
            changed = True
    messages = out.get("messages")
    if isinstance(messages, list):
        new_messages = []
        messages_changed = False
        for message in messages:
            updated, did = _strip_message(message)
            new_messages.append(updated)
            messages_changed = messages_changed or did
        if messages_changed:
            out["messages"] = new_messages
            changed = True
    if not changed:
        return request, False
    return out, True


def _strip_message(message: Any) -> tuple[Any, bool]:
    if not isinstance(message, dict):
        return message, False
    role = message.get("role")
    content = message.get("content")
    new_content, did = _strip_content(content, system=(role == "system"))
    if not did:
        return message, False
    return {**message, "content": new_content}, True


def _strip_content(content: Any, *, system: bool) -> tuple[Any, bool]:
    if isinstance(content, str):
        if system:
            return strip_skills_index(content)
        stripped = _DELTA_NOTE.sub("", content)
        return (stripped, True) if stripped != content else (content, False)
    if isinstance(content, list):
        changed = False
        parts = []
        for part in content:
            if isinstance(part, str):
                updated, did = _strip_content(part, system=system)
                parts.append(updated)
                changed = changed or did
                continue
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                updated, did = _strip_content(part["text"], system=system)
                if did:
                    parts.append({**part, "text": updated})
                    changed = True
                else:
                    parts.append(part)
                continue
            parts.append(part)
        return (parts, True) if changed else (content, False)
    return content, False


def _expand_start(text: str, block_start: int) -> int:
    window_start = max(0, block_start - 6000)
    region = text[window_start:block_start]
    heading = None
    for match in _SKILLS_HEADING.finditer(region):
        heading = match
    if heading is None:
        return block_start
    between = region[heading.end() :]
    if _OTHER_HEADING.search(between):
        return block_start
    return window_start + heading.start()


def _expand_end(text: str, block_end: int) -> int:
    rest = text[block_end:]
    lines = rest.splitlines(keepends=True)
    index = 0
    while index < len(lines) and lines[index].strip() == "":
        index += 1
    cursor = index
    while cursor < len(lines):
        lowered = lines[cursor].strip().lower()
        if any(lowered.startswith(prefix) for prefix in _EPILOGUE_PREFIXES):
            cursor += 1
            continue
        break
    if cursor == index:
        return block_end
    while cursor < len(lines) and lines[cursor].strip() == "":
        cursor += 1
    return block_end + sum(len(lines[pos]) for pos in range(cursor))
