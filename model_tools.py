"""Two tools that let the model find and load skills itself ("google your skills").

Why two small tools and not a list in the prompt: a model only uses a skill it
knows exists, but a list of thousands (Hermes' ``<available_skills>``) costs
context on every turn. A tool schema costs ~200 tokens once, a search costs
nothing until the model calls it, and its answer is a few lines.

* ``skill_search(query)`` - names and one-line descriptions, best first, with
  a match score and whether the skill is already loaded. Loads nothing.
* ``skill_load(names)``   - the full text of the chosen skills (and makes them
  allowed for ``skill_view`` in this chat). Loaded skills join the fan.

Setting ``model_tools`` (default on) turns both off. The user's magazine is
unaffected either way.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger("triage")

TOOLSET = "triage"
LOAD_BUDGET_CHARS = 40_000
_DESC_CHARS = 160

SEARCH_SCHEMA = {
    "name": "skill_search",
    "description": (
        "Search the user's local skill library: thousands of expert playbooks for frameworks, "
        "platforms, languages, file formats, tools and workflows (Android, Kotlin, game dev, "
        "Git, design, writing ...). None of them is in your context until you load it. "
        "Search before you start any specialised task, and whenever the user asks you to find "
        "or use skills. Returns names with one-line descriptions; nothing is loaded. "
        "Then call skill_load with the names you will actually use."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What you need, in plain words or a skill name (e.g. 'android crash debugging', 'kotlin coroutines', '2d-games').",
            },
            "limit": {"type": "integer", "description": "How many results (default 8, max 20).", "minimum": 1, "maximum": 20},
        },
        "required": ["query"],
    },
}

LOAD_SCHEMA = {
    "name": "skill_load",
    "description": (
        "Load skills found with skill_search: returns each skill's full instructions, which you "
        "should then follow. Load only what you will use (each one costs context). Already "
        "loaded skills are not sent again."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "names": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Exact skill names from skill_search (up to 10).",
                "maxItems": 10,
            },
        },
        "required": ["names"],
    },
}


def _session(kwargs: dict) -> str:
    return str(kwargs.get("session_id") or kwargs.get("task_id") or "")


def skill_search(args: dict, **kwargs) -> str:
    from .context import name_search
    from .service import current_runtime

    runtime = current_runtime()
    if runtime is None:
        return json.dumps({"error": "triage is not loaded"})
    query = str((args or {}).get("query") or "").strip()
    if not query:
        return json.dumps({"error": "query is required"})
    try:
        limit = max(1, min(int((args or {}).get("limit") or 8), 20))
    except (TypeError, ValueError):
        limit = 8
    session_id = _session(kwargs)
    found: dict[str, tuple[float, str]] = {}
    for row in name_search(runtime.store, query.replace(" ", "-"), limit=limit):
        found[row["name"]] = (float(row["probability"]), row["description"])
    ranking = runtime.judge.rank(query, limit=limit, independent=True)
    for item in ranking.candidates:
        if item.name not in found or found[item.name][0] < item.probability:
            found[item.name] = (item.probability, item.description)
    loaded = {name.casefold() for name in runtime.ledger.session_names(session_id)}
    ordered = sorted(found.items(), key=lambda pair: -pair[1][0])[:limit]
    results = [
        {
            "name": name,
            "description": (description or "")[:_DESC_CHARS],
            "match": f"{round(score * 100)}%",
            "loaded": name.casefold() in loaded,
        }
        for name, (score, description) in ordered
    ]
    return json.dumps(
        {
            "results": results,
            "next": "Nothing is loaded yet. Call skill_load(names=[...]) with the ones you will use."
            if results else "No skill matched. Try other words, or continue without a skill.",
        },
        ensure_ascii=False,
    )


def skill_load(args: dict, **kwargs) -> str:
    from .catalog_io import skill_markdown
    from .service import current_runtime, scope_key

    runtime = current_runtime()
    if runtime is None:
        return json.dumps({"error": "triage is not loaded"})
    raw = (args or {}).get("names") or []
    if isinstance(raw, str):
        raw = [raw]
    names = [str(name).strip() for name in raw if str(name).strip()][:10]
    if not names:
        return json.dumps({"error": "names is required"})
    session_id = _session(kwargs)
    already = {name.casefold() for name in runtime._inlined.get(session_id, set())}
    loaded, skipped, unknown, deferred = [], [], [], []
    used = 0
    for name in names:
        skill = runtime.store.get(name)
        if skill is None:
            unknown.append(name)
            continue
        runtime.ledger.approve(scope_key("", session_id), [skill.name], session_id)
        if skill.name.casefold() in already:
            skipped.append(skill.name)
            continue
        text = skill_markdown(skill)
        if used + len(text) > LOAD_BUDGET_CHARS and loaded:
            deferred.append(skill.name)
            continue
        used += len(text)
        loaded.append({"name": skill.name, "instructions": text})
        runtime._inlined.setdefault(session_id, set()).add(skill.name.casefold())
        runtime.store.bump_use(skill.name)
        runtime._log("", session_id, "skill_load", [], skill.name, True, None, "model loaded")
    payload: dict = {"loaded": loaded}
    if skipped:
        payload["already_loaded"] = skipped
    if deferred:
        payload["load_next"] = f"Too much for one call; load these next: {', '.join(deferred)}."
    if unknown:
        payload["unknown"] = f"Not in the library: {', '.join(unknown)}. Use skill_search for exact names."
    return json.dumps(payload, ensure_ascii=False)


def register_model_tools(ctx) -> bool:
    """Register both tools. False when the host has no tool API."""
    register = getattr(ctx, "register_tool", None)
    if register is None:
        return False
    try:
        register(name="skill_search", toolset=TOOLSET, schema=SEARCH_SCHEMA, handler=skill_search,
                 description="Search the local skill library", emoji="🔎")
        register(name="skill_load", toolset=TOOLSET, schema=LOAD_SCHEMA, handler=skill_load,
                 description="Load skills from the local library", emoji="📚")
        return True
    except Exception:
        logger.exception("triage: model tools not registered")
        return False
