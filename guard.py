"""Refuse ``skill_view`` unless this turn approved the skill name."""

from __future__ import annotations

from typing import Any, Callable

SKILL_VIEW_TOOL = "skill_view"


def normalize_skill_name(name: str) -> str:
    text = (name or "").strip()
    if ":" in text:
        text = text.split(":")[-1]
    return text.casefold()


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
            f"triage: skill_view({shown!r}) is not approved for this session. "
            "Pick a probability-tagged skill with /triage, reply with its name "
            "or number, or raise auto_accept_threshold. Unapproved loads stay closed."
        ),
    }
