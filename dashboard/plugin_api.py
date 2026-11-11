"""Rank, catalog, and desktop approval routes.

``POST /approve`` stores a pending force in the catalog database. The live
plugin pops that row on the next ``pre_llm_call``, the same way ``/triage use``
does. Desktop cannot rely on ``slash.exec`` alone: a local session often has
no gateway session-map entry.
"""

from __future__ import annotations

import importlib
import sqlite3
import sys
import types
from pathlib import Path

from fastapi import APIRouter, Query

router = APIRouter()
_ROOT = Path(__file__).resolve().parents[1]
_PKG = "_triage_api"


def _pkg():
    existing = sys.modules.get(_PKG)
    if existing is None or not getattr(existing, "__path__", None):
        package = types.ModuleType(_PKG)
        package.__path__ = [str(_ROOT)]  # type: ignore[attr-defined]
        package.__package__ = _PKG
        sys.modules[_PKG] = package
    return _PKG


def _mod(name: str):
    return importlib.import_module(f"{_pkg()}.{name}")


def _store():
    paths = _mod("paths")
    store_mod = _mod("store")
    return store_mod.SkillStore(paths.default_db_path())


@router.get("/rank")
async def rank(
    q: str = Query("", max_length=4000),
    session_id: str = Query("", max_length=200),
    limit: int = Query(0, ge=0, le=8),
    held: str = Query("", max_length=20000),
    exclude: str = Query("", max_length=20000),
    spare: int = Query(0, ge=0, le=24),
):
    """Desktop chips: the draft plus what the session is about.

    With ``session_id`` the chips also follow the conversation (recent turns,
    the first prompt and its attachments, the workspace's AGENTS.md/README),
    and an empty draft still returns conversation chips. Without it, only the
    draft is ranked, as before.
    """
    text = (q or "").strip()
    store = _store()
    try:
        judge = _mod("judge").SkillJudge(store)
        if session_id:
            store.mark_chip_session(session_id)
            # Held and already-loaded skills are resolved here, not sent by the
            # desktop (a long pile would not fit in a URL).
            held_names = store.get_pending_attach(session_id)
            loaded_names = [str(row["chosen"]) for row in _accepted_rows(store.db_path, session_id)]
            loaded_names += [load["name"] for load in _history_loads([session_id]) if load["state"] == "in"]
            held = ",".join(_names(held) + held_names)
            always = _mod("preload").parse_names(_plugin_setting("always_load", ""))
            exclude = ",".join(_names(exclude) + held_names + loaded_names + list(always))
        size = limit or _plugin_setting("shortlist_size", 5)
        try:
            size = max(1, min(int(size), 8))
        except (TypeError, ValueError):
            size = 4
        if session_id:
            result = _mod("context").chip_ranking(
                judge, text, session_id, limit=size, held=_names(held), exclude=_names(exclude), spare=spare
            )
        elif text:
            ranking = judge.rank(text, limit=size)
            result = {
                "candidates": [
                    {
                        "name": item.name,
                        "description": item.description,
                        "probability": item.probability,
                        "why": item.why,
                        "label": item.label(),
                        "source": "draft",
                    }
                    for item in ranking.candidates
                ],
                "none_probability": ranking.none_probability,
            }
        else:
            return {"candidates": [], "none_probability": 1.0}
        catalog = {skill.name.casefold(): skill for skill in store.list_skills()}
    finally:
        store.close()
    for row in result["candidates"]:
        skill = catalog.get(str(row["name"]).casefold())
        row["tags"] = list(skill.tags) if skill else []
        row["origin_path"] = skill.origin_path if skill else ""
    return result


@router.get("/loaded")
async def loaded(session_id: str = ""):
    """Skills loaded in this chat (and the chats it was compacted from).

    Each carries ``state``: ``in`` (in context), ``released`` (last loaded
    before the latest compaction, so its text left the context) or
    ``reloaded`` (loaded before a compaction and again after it)."""
    store = _store()
    try:
        skills = {skill.name.casefold(): skill for skill in store.list_skills()}
    finally:
        store.close()
    chain = _session_chain(store.db_path, session_id)
    rows = _loaded_rows(store.db_path, chain)
    compacted_at = _compacted_at(store.db_path, chain)
    compacted = _session_compacted(store.db_path, session_id)
    items = []
    for index, row in enumerate(rows, start=1):
        skill = skills.get(str(row["chosen"]).casefold())
        first, last = str(row["first_at"]), str(row["last_at"])
        if compacted_at and last < compacted_at:
            state = "released"
        elif compacted_at and first < compacted_at <= last:
            state = "reloaded"
        else:
            state = "in"
        items.append({
            "name": row["chosen"],
            "description": skill.description if skill else "",
            "tags": list(skill.tags) if skill else [],
            "origin_path": skill.origin_path if skill else "",
            "turn": index,
            "accepted_at": last,
            "state": state,
            "bridged": state == "released",
            "injected": state != "released",
        })
    known = {str(item["name"]).casefold() for item in items}
    for load in _history_loads(chain):
        key = load["name"].casefold()
        if key in known:
            continue
        known.add(key)
        skill = skills.get(key)
        items.append({
            "name": skill.name if skill else load["name"],
            "description": skill.description if skill else "",
            "tags": list(skill.tags) if skill else [],
            "origin_path": skill.origin_path if skill else "",
            "turn": 0,
            "accepted_at": load["at"],
            "state": load["state"],
            "bridged": load["state"] == "released",
            "injected": load["state"] != "released",
            "source": "history",
        })
    sticky_store = _store()
    try:
        sticky = {name.casefold() for name in sticky_store.sticky(chain)}
    finally:
        sticky_store.close()
    for item in items:
        item["sticky"] = str(item["name"]).casefold() in sticky
    items.sort(key=lambda item: str(item["accepted_at"]))
    for index, item in enumerate(items, start=1):
        item["turn"] = index
    return {"skills": items, "compacted": compacted, "compacted_at": compacted_at}


def _history_loads(session_ids: list[str]) -> list[dict]:
    """Skills the chat loaded through Hermes itself (see context.history_loads)."""
    try:
        return list(_mod("context").history_loads(session_ids))
    except Exception:
        return []


def _family(session_id: str) -> list[str]:
    """Other ids of the same chat in Hermes' session table (parents, then children)."""
    try:
        return list(_mod("context").session_family(session_id))
    except Exception:
        return []


def _session_chain(db_path: Path, session_id: str, depth: int = 8) -> list[str]:
    chain = [session_id] if session_id else []
    if not chain or not db_path.exists():
        return chain + [other for other in _family(session_id) if other not in chain] if chain else chain
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            current = session_id
            for _ in range(depth):
                row = conn.execute("SELECT parent_id FROM session_parents WHERE session_id = ?", (current,)).fetchone()
                if not row or not row[0] or row[0] in chain:
                    break
                chain.append(str(row[0]))
                current = str(row[0])
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    for other in _family(session_id):
        if other not in chain:
            chain.append(other)
    return chain


def _loaded_rows(db_path: Path, chain: list[str]) -> list[sqlite3.Row]:
    if not chain or not db_path.exists():
        return []
    marks = ",".join("?" for _ in chain)
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            f"""
            SELECT chosen, min(created_at) AS first_at, max(created_at) AS last_at, min(id) AS first_id
              FROM selections
             WHERE accepted = 1 AND chosen IS NOT NULL AND chosen != '' AND session_id IN ({marks})
             GROUP BY lower(chosen)
             ORDER BY first_id ASC
            """,
            tuple(chain),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _compacted_at(db_path: Path, chain: list[str]) -> str:
    if not chain or not db_path.exists():
        return ""
    marks = ",".join("?" for _ in chain)
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                f"SELECT max(compacted_at) FROM session_flags WHERE session_id IN ({marks})", tuple(chain)
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return ""
    return str(row[0]) if row and row[0] else ""


@router.get("/skill/{name}")
async def skill_body(name: str):
    store = _store()
    try:
        skill = store.get(name)
    finally:
        store.close()
    if skill is None:
        return {"success": False, "error": f"Unknown skill {name!r}."}
    return {
        "success": True,
        "name": skill.name,
        "description": skill.description,
        "body": skill.body,
        "tags": list(skill.tags),
    }


@router.post("/approve")
async def approve(body: dict):
    name = str((body or {}).get("name") or "").strip()
    session_id = str((body or {}).get("session_id") or "")
    if not name:
        return {"ok": False, "error": "name is required."}
    why = str((body or {}).get("why") or "desktop approve")
    try:
        probability = float((body or {}).get("probability") or 0)
    except (TypeError, ValueError):
        probability = 0.0
    store = _store()
    try:
        skill = store.get(name)
        if skill is None:
            return {"ok": False, "error": f"Unknown skill {name!r}."}
        store.enqueue_force(session_id, skill.name, probability, why)
        store.log_selection(
            turn_id="",
            session_id=session_id,
            query="",
            candidates=[{"name": skill.name, "p": probability, "why": why}],
            chosen=skill.name,
            accepted=True,
        )
        _try_live_force(session_id, skill.name)
    finally:
        store.close()
    return {"ok": True, "name": skill.name, "session_id": session_id}


def _names(value: str) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


@router.get("/search")
async def search(
    q: str = Query("", max_length=200),
    session_id: str = Query("", max_length=200),
    limit: int = Query(8, ge=1, le=20),
):
    """Atomic skill search for a typed ``/term`` (name first, typo-tolerant)."""
    store = _store()
    try:
        exclude = store.get_pending_attach(session_id) if session_id else []
        rows = _mod("context").name_search(store, q, limit=limit, exclude=exclude)
    finally:
        store.close()
    return {"candidates": rows, "query": q}


def _favorites_payload(store) -> dict:
    return {"favorites": store.favorites(), "most_used": store.most_used()}


@router.get("/favorites")
async def get_favorites():
    """Starred skills and the most used ones, for the empty "More" list."""
    store = _store()
    try:
        return _favorites_payload(store)
    finally:
        store.close()


@router.post("/favorites")
async def post_favorites(body: dict):
    name = str((body or {}).get("name") or "").strip()
    store = _store()
    try:
        skill = store.get(name) if name else None
        if skill is None:
            return {"ok": False, "error": f"Unknown skill {name!r}.", **_favorites_payload(store)}
        store.set_favorite(skill.name, bool((body or {}).get("on")))
        return {"ok": True, **_favorites_payload(store)}
    finally:
        store.close()


@router.post("/sticky")
async def post_sticky(body: dict):
    """Mark a skill of this chat to come back after a compaction (or clear the mark)."""
    name = str((body or {}).get("name") or "").strip()
    session_id = str((body or {}).get("session_id") or "")
    if not name or not session_id:
        return {"ok": False, "error": "name and session_id are required."}
    store = _store()
    try:
        on = bool((body or {}).get("on"))
        ids = [session_id] + _family(session_id)
        if on:
            store.set_sticky(session_id, name, True)
        else:
            for sid in ids:
                store.set_sticky(sid, name, False)
        return {"ok": True, "name": name, "on": on}
    finally:
        store.close()


@router.get("/attach")
async def get_attach(session_id: str = ""):
    store = _store()
    try:
        for sid in [session_id] + _family(session_id):
            names = store.get_pending_attach(sid)
            if names:
                return {"names": names, "autos": store.get_pending_autos(sid)}
        return {"names": [], "autos": []}
    finally:
        store.close()


@router.post("/attach")
async def post_attach(body: dict):
    """The desktop's left pile: skills to load with this chat's next message.

    Kept outside the prompt text; the agent process takes them in pre_llm."""
    session_id = str((body or {}).get("session_id") or "")
    raw = (body or {}).get("names") or []
    limit = _max_attached() or 10**9
    store = _store()
    try:
        names: list[str] = []
        unknown: list[str] = []
        for item in raw if isinstance(raw, list) else []:
            skill = store.get(str(item or "").strip())
            if skill is None:
                unknown.append(str(item))
            elif skill.name not in names and len(names) < limit:
                names.append(skill.name)
        raw_autos = (body or {}).get("autos") or []
        store.set_pending_attach(
            session_id,
            names,
            fresh=bool((body or {}).get("fresh")),
            autos=[str(item) for item in raw_autos] if isinstance(raw_autos, list) else [],
        )
        if session_id:
            store.mark_chip_session(session_id)
    finally:
        store.close()
    return {"ok": True, "names": names, "unknown": unknown, "max_attached": _max_attached()}


def _max_attached() -> int:
    """0 = no limit (the default)."""
    try:
        return max(0, int(_plugin_setting("max_attached", 0)))
    except (TypeError, ValueError):
        return 0


@router.get("/settings")
async def get_settings():
    try:
        size = max(1, min(int(_plugin_setting("shortlist_size", 5)), 8))
    except (TypeError, ValueError):
        size = 5
    return {
        "auto_accept_threshold": _read_threshold(),
        "shortlist_size": size,
        "max_attached": _max_attached(),
        "debug": bool(_plugin_setting("debug", False)),
        "catalog_version": _catalog_version(),
    }


def _catalog_version() -> str:
    """Changes whenever a skill is added, removed or edited (the app re-downloads /names)."""
    store = _store()
    try:
        row = store._conn.execute(
            "SELECT count(*), coalesce(max(id), 0), coalesce(max(updated_at), '') FROM skills"
        ).fetchone()
    finally:
        store.close()
    return ":".join(str(part) for part in tuple(row))


@router.get("/names")
async def names():
    """Every skill's name and short description, for the app's own instant /term search."""
    store = _store()
    try:
        skills = store.list_skills()
    finally:
        store.close()
    return {
        "version": _catalog_version(),
        "skills": [[skill.name, (skill.description or "")[:140]] for skill in skills],
    }


@router.post("/settings")
async def post_settings(body: dict):
    enabled = body.get("enabled", True)
    raw = body.get("auto_accept_threshold", 0.90)
    try:
        number = float(raw)
    except (TypeError, ValueError):
        number = 0.90
    number = 0.0 if enabled is False else max(0.0, min(1.0, number))
    try:
        from hermes_cli.plugins_state import save_plugin_setting

        save_plugin_setting(_mod("paths").PLUGIN_NAME, ("auto_accept_threshold",), number)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "auto_accept_threshold": _read_threshold()}
    return {"ok": True, "auto_accept_threshold": number}


def _try_live_force(session_id: str, name: str) -> bool:
    """Best-effort in-process Runtime.memory.force when the agent plugin is loaded here."""
    try:
        runtime = _mod("service").current_runtime()
        if runtime is None:
            return False
        runtime.memory.force(session_id, name, 0.0, "desktop approve")
        return True
    except Exception:
        return False


def _plugin_setting(key: str, default):
    """One value from plugins.entries.<name>.settings (current name, then the legacy one)."""
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
        entries = ((cfg.get("plugins") or {}).get("entries") or {})
        for name in (_mod("paths").PLUGIN_NAME, *_mod("paths").LEGACY_NAMES):
            entry = entries.get(name) or {}
            settings = entry.get("settings") or entry.get("config") or {}
            if key in settings:
                return settings[key]
    except Exception:
        pass
    return default


def _read_threshold() -> float:
    try:
        return max(0.0, min(1.0, float(_plugin_setting("auto_accept_threshold", 0.90))))
    except (TypeError, ValueError):
        return 0.90


def _session_compacted(db_path: Path, session_id: str) -> bool:
    if not session_id or not db_path.exists():
        return False
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        row = conn.execute(
            "SELECT compacted FROM session_flags WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    return bool(row and row[0])


def _accepted_rows(db_path: Path, session_id: str) -> list[sqlite3.Row]:
    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return []
    conn.row_factory = sqlite3.Row
    if not session_id:
        return []  # a chat without an id has loaded nothing (never "all chats")
    try:
        if session_id:
            ids = [session_id] + _family(session_id)
            marks = ",".join("?" for _ in ids)
            found = conn.execute(
                f"""
                SELECT chosen, created_at FROM selections
                 WHERE accepted = 1 AND chosen IS NOT NULL AND chosen != ''
                   AND session_id IN ({marks})
                 ORDER BY id ASC
                """,
                tuple(ids),
            ).fetchall()
        else:
            found = conn.execute(
                """
                SELECT chosen, created_at FROM selections
                 WHERE accepted = 1 AND chosen IS NOT NULL AND chosen != ''
                 ORDER BY id ASC
                """
            ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    latest: dict[str, sqlite3.Row] = {}
    for row in found:
        latest[str(row["chosen"]).casefold()] = row
    return list(latest.values())
