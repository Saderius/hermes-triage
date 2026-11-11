"""Opt-in edits that empty the Hermes skills index inputs.

These helpers change the user's profile config and skills directory only when
the migrate command is asked to. They do not patch Hermes source.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ConfigRewrite:
    text: str
    changed: bool
    warning: str = ""


_LIST_KEYS = ("auto_load", "external_dirs")


def rewrite_skills_config(text: str) -> ConfigRewrite:
    """Set ``skills.auto_load`` and ``skills.external_dirs`` to empty lists.

    Handles the block-style ``config.yaml`` Hermes writes. An inline
    ``skills: {…}`` mapping is left untouched and reported as a warning.
    """
    newline = "\r\n" if "\r\n" in text else "\n"
    ends_with_newline = text.endswith(("\n", "\r\n"))
    lines = text.splitlines()
    skills_at: int | None = None
    skills_indent = 0
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)skills\s*:\s*(.*?)\s*$", line)
        if not match or line.strip().startswith("#"):
            continue
        skills_at = index
        skills_indent = len(match.group(1))
        inline = match.group(2)
        if inline and not inline.startswith("#"):
            return ConfigRewrite(
                text=text,
                changed=False,
                warning=(
                    "skills: is an inline mapping. Set auto_load: [] and "
                    "external_dirs: [] by hand; --apply-config left the file unchanged."
                ),
            )
        break
    if skills_at is None:
        block = ["skills:", "  auto_load: []", "  external_dirs: []"]
        base = lines
        if base and base[-1] != "":
            base = [*base, ""]
        merged = [*base, *block]
        rendered = newline.join(merged)
        if ends_with_newline or True:
            rendered += newline
        return ConfigRewrite(text=rendered, changed=True)

    end = len(lines)
    for index in range(skills_at + 1, len(lines)):
        line = lines[index]
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= skills_indent:
            end = index
            break
    section = _blank_list_keys(lines[skills_at + 1 : end], skills_indent)
    new_lines = [*lines[: skills_at + 1], *section, *lines[end:]]
    rendered = newline.join(new_lines)
    if ends_with_newline:
        rendered += newline
    return ConfigRewrite(text=rendered, changed=rendered != text)


def cut_snippet() -> str:
    return (
        "skills:\n"
        "  auto_load: []\n"
        "  external_dirs: []\n"
        "\n"
        "# Then stop bundled re-seeding (v0.22.7 still copies ESSENTIAL_SKILLS,\n"
        "# notably hermes-agent; newer Hermes skips the seed entirely):\n"
        "#   hermes skills opt-out\n"
        "# or write $HERMES_HOME/.no-bundled-skills\n"
        "#\n"
        "# Project skills under <repo>/.hermes/skills and .agents/skills are a\n"
        "# separate root. Move those aside too if this checkout vendors skills.\n"
        "# The llm_request middleware strips a leftover <available_skills> block\n"
        "# from the outbound system message either way.\n"
    )


def write_opt_out_marker(profile_home: Path) -> Path:
    profile_home.mkdir(parents=True, exist_ok=True)
    marker = profile_home / ".no-bundled-skills"
    marker.write_text(
        "This profile opted out of bundled-skill seeding.\n"
        "Written by triage migrate --empty-roots.\n"
        "Delete this file (or run `hermes skills opt-in`) to let Hermes seed skills again.\n",
        encoding="utf-8",
    )
    return marker


def _blank_list_keys(section: list[str], parent_indent: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    index = 0
    while index < len(section):
        line = section[index]
        match = re.match(r"^(\s*)([A-Za-z0-9_]+)\s*:(.*)$", line)
        if (
            match
            and len(match.group(1)) == parent_indent + 2
            and match.group(2) in _LIST_KEYS
        ):
            key = match.group(2)
            seen.add(key)
            comment = ""
            rest = match.group(3)
            hash_at = rest.find("#")
            if hash_at != -1:
                comment = " " + rest[hash_at:].rstrip()
            out.append(f"{match.group(1)}{key}: []{comment}")
            index += 1
            key_indent = len(match.group(1))
            while index < len(section):
                nxt = section[index]
                if not nxt.strip():
                    lookahead = index + 1
                    while lookahead < len(section) and not section[lookahead].strip():
                        lookahead += 1
                    if lookahead < len(section):
                        nxt_indent = len(section[lookahead]) - len(section[lookahead].lstrip(" "))
                        if nxt_indent > key_indent:
                            index = lookahead
                            continue
                    break
                nxt_indent = len(nxt) - len(nxt.lstrip(" "))
                if nxt_indent > key_indent:
                    index += 1
                    continue
                break
            continue
        out.append(line)
        index += 1
    indent = " " * (parent_indent + 2)
    for key in _LIST_KEYS:
        if key not in seen:
            out.append(f"{indent}{key}: []")
    return out
