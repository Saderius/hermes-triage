"""Refuse ``skill_view`` unless this turn approved the skill name."""

from __future__ import annotations

from typing import Any, Callable

SKILL_VIEW_TOOL = "skill_view"


def bare_skill_name(name: str) -> str:
    """``plugin:name``, ``category/name`` and ``category\\name`` all mean ``name``.

    Models address Hermes' own skills by their folder path; the catalog knows
    them by the folder's name."""
    text = (name or "").strip()
    for sep in (":", "/", "\\"):
        if sep in text:
            text = text.split(sep)[-1]
    return text.strip()


def normalize_skill_name(name: str) -> str:
    return bare_skill_name(name).casefold()


def skill_name_from_args(args: Any) -> str:
    if not isinstance(args, dict):
        return ""
    for key in ("name", "skill", "skill_name"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def evaluate_skill_view(
    tool_name: str,
    args: Any,
    *,
    is_approved: Callable[[str], bool],
) -> dict[str, str] | None:
    """Return a ``pre_tool_call`` block dict, or ``None`` to allow the call."""
    if tool_name != SKILL_VIEW_TOOL:
        return None
    raw_name = skill_name_from_args(args)
    normalized = normalize_skill_name(raw_name)
    if normalized and is_approved(normalized):
        return None
    shown = raw_name or "(missing name)"
    return {
        "action": "block",
        "message": (
            f"triage: skill_view({shown!r}) is not approved for this chat. "
            "Load the skill first with skill_load (or the user picks it from the "
            "choices Triage showed); after that skill_view works for it. "
            "Unapproved loads stay closed."
        ),
    }
