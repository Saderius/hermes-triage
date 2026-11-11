"""Conversation context for ranking: what the chat is about, not just the draft.

The judge's chips should follow the line of thought — the user's messages, the
assistant's replies, what tools read, and whatever the session started with
(the first prompt, its attachments, and the workspace's AGENTS.md / README).

Sources:

* Hermes' ``state.db`` (read-only): recent rows of one session plus its first
  user message. ``api_content`` is preferred for user rows because it carries
  expanded attachments; blocks injected by skill plugins (this one included)
  are cut so the chips never rank their own output.
* The session's workspace (``sessions.cwd``): AGENTS.md, CLAUDE.md, HERMES.md
  and README.md, clipped.

Everything here fails soft: a missing database or file yields empty context.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .paths import hermes_home
from .judge import _skill_text, tokenize

PROJECT_FILES = ("AGENTS.md", "CLAUDE.md", "HERMES.md", ".hermes.md", "README.md")
_PROJECT_CLIP = 3000
_RECENT_ROWS = 24

# Plugin-injected blocks appended to a user's api_content. Cut from the first
# marker to the end of the message.
_INJECTED = re.compile(
    r"\n*(?:\[(?:triage|hermes-skill-judge)\]|## Retrieved Skills\b|\[System: The skills available)",
)


@dataclass
class SessionContext:
    first_user: str = ""
    users: list[str] = field(default_factory=list)
    assistants: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    cwd: str = ""

    @property
    def latest_user(self) -> str:
        return self.users[-1] if self.users else ""


def strip_injected(text: str) -> str:
    if not text:
        return ""
    match = _INJECTED.search(text)
    return (text[: match.start()] if match else text).strip()


# Hermes' own notes at the head of a message, in square brackets: "[System: the
# model changed ...]", "[Context from the interrupted assistant response]",
# "[PRIOR CONTEXT — for reference only ...]". They are not what the user said,
# and their words ("context", "interrupted", "restore") pulled in skills about
# context management, above all after a compression left few real messages.
_LEAD_NOTE = re.compile(r"\A\s*\[[^\[\]]{1,1500}\]\s*")
_SKILL_INVOKED = "[IMPORTANT: The user has invoked the "
_SKILL_INSTRUCTION = re.compile(
    r"(?:The user has provided the following instruction alongside the skill invocation:|User instruction:)\s*(.*)\Z",
    re.S,
)
_COMPACTION = "[CONTEXT COMPACTION"
_COMPACTION_GOAL = re.compile(r"^## Goal\s*\n(.*?)(?=^## |\Z)", re.S | re.M)


def own_words(text: str) -> str:
    """What the user (or assistant) wrote, without Hermes' bracketed notes.

    * A skill invocation keeps only the instruction typed with it; the skill's
      own text is not the user's words.
    * A compaction note keeps only its "## Goal" section.
    * Other leading ``[...]`` notes are cut; a message that was only notes
      becomes empty."""
    text = (text or "").strip()
    if text.startswith(_SKILL_INVOKED):
        match = _SKILL_INSTRUCTION.search(text)
        return match.group(1).strip() if match else ""
    if text.startswith(_COMPACTION):
        match = _COMPACTION_GOAL.search(text)
        return match.group(1).strip() if match else ""
    for _ in range(4):
        match = _LEAD_NOTE.match(text)
        if not match:
            break
        text = text[match.end():]
    return text.strip()


def state_db_path() -> Path:
    return hermes_home() / "state.db"


def session_context(session_id: str, *, db_path: Path | None = None, rows: int = _RECENT_ROWS) -> SessionContext:
    """Recent turns of one session, oldest first, plus its first user message and cwd."""
    out = SessionContext()
    if not session_id:
        return out
    path = db_path or state_db_path()
    if not path.exists():
        return out
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return out
    conn.row_factory = sqlite3.Row
    try:
        try:
            row = conn.execute("SELECT cwd FROM sessions WHERE id = ?", (session_id,)).fetchone()
            out.cwd = str(row["cwd"] or "") if row else ""
        except sqlite3.Error:
            out.cwd = ""
        firsts = conn.execute(
            """
            SELECT content, api_content FROM messages
             WHERE session_id = ? AND role = 'user' AND active = 1
             ORDER BY id ASC LIMIT 6
            """,
            (session_id,),
        ).fetchall()
        # The first message with words of its own (a note alone does not count).
        for first in firsts:
            text = own_words(strip_injected(first["api_content"] or first["content"] or ""))
            if text:
                out.first_user = _clip(text, 4000)
                break
        recent = conn.execute(
            """
            SELECT role, content, api_content FROM messages
             WHERE session_id = ? AND active = 1
             ORDER BY id DESC LIMIT ?
            """,
            (session_id, int(rows)),
        ).fetchall()
    except sqlite3.Error:
        return out
    finally:
        conn.close()
    for item in reversed(recent):
        role = str(item["role"] or "")
        if role == "user":
            text = own_words(strip_injected(item["api_content"] or item["content"] or ""))
            if text:
                out.users.append(_clip(text, 2000))
        elif role == "assistant":
            text = own_words(item["content"] or "")
            if text:
                out.assistants.append(_clip(text, 2500))
        elif role == "tool":
            text = (item["content"] or "").strip()
            if text:
                out.tools.append(_clip(text, 1500))
    return out


# Skills a chat loaded outside Triage, as Hermes wrote them into its history.
_INVOKED_ONE = re.compile(r'\[IMPORTANT: The user has invoked the "([^"/][^"]*)" skill')
_INVOKED_MANY = re.compile(r"Skills loaded: ([A-Za-z0-9_:., -]+?)(?= User instruction:| \[Loaded as part|\n|$)")
_VIEWED = re.compile(r'\s*\{"success": true, "name": "([^"]+)"')
# What Hermes leaves of a skill_view result once a compaction pruned its text.
_PRUNED = re.compile(r"\[skill_view\] name=(\S+) \(")
_SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
# (state.db, session ids, row count / newest id / active rows) -> loads
_HISTORY_CACHE: dict = {}


def _history_names(role: str, tool_name: str, content: str) -> list[str]:
    names: list = []
    if role == "user":
        head = content[:4000]
        if "stacked skill bundle" in head[:400]:
            match = _INVOKED_MANY.search(head)
            names = match.group(1).split(",") if match else []
        else:
            names = _INVOKED_ONE.findall(head[:400])
    elif tool_name == "skill_view":
        match = _VIEWED.match(content[:400]) or _PRUNED.match(content[:400])
        names = [match.group(1)] if match else []
    elif tool_name == "skill_load":
        try:
            data = json.loads(content)
        except ValueError:
            data = None
        loaded = data.get("loaded") if isinstance(data, dict) else None
        names = [item.get("name") for item in loaded or [] if isinstance(item, dict)]
    out = []
    for name in names:
        # "triage:name" and "category\name" both mean the skill "name".
        name = re.split(r"[:\\/]", str(name or "").strip())[-1]
        if _SKILL_NAME.match(name):
            out.append(name)
    return out


def history_loads(session_ids: list[str], *, db_path: Path | None = None) -> list[dict]:
    """Skills these sessions loaded through Hermes itself, oldest first.

    Each is ``{"name", "state", "at"}``. ``state`` is ``"in"`` when a message
    that loaded it is still in the chat's context, ``"released"`` when every
    such message was compacted away. ``at`` is the first load, ISO 8601 UTC."""
    ids = [str(sid) for sid in session_ids if sid]
    path = db_path or state_db_path()
    if not ids or not path.exists():
        return []
    marks = ",".join("?" for _ in ids)
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return []
    conn.row_factory = sqlite3.Row
    try:
        stamp = conn.execute(
            f"SELECT count(*), coalesce(max(id), 0), coalesce(sum(active), 0) FROM messages WHERE session_id IN ({marks})",
            tuple(ids),
        ).fetchone()
        key = (str(path), tuple(ids), tuple(stamp))
        hit = _HISTORY_CACHE.get(key)
        if hit is not None:
            return [dict(item) for item in hit]
        rows = conn.execute(
            f"""
            SELECT role, tool_name, content, active, timestamp FROM messages
             WHERE session_id IN ({marks})
               AND (tool_name IN ('skill_view', 'skill_load')
                    OR (role = 'user' AND content LIKE '[IMPORTANT: The user has invoked the %'))
             ORDER BY id ASC
            """,
            tuple(ids),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    found: dict[str, dict] = {}
    for row in rows:
        for name in _history_names(str(row["role"] or ""), str(row["tool_name"] or ""), str(row["content"] or "")):
            try:
                at = datetime.fromtimestamp(float(row["timestamp"] or 0), timezone.utc).isoformat(timespec="seconds")
            except (TypeError, ValueError, OSError):
                at = ""
            item = found.setdefault(name.casefold(), {"name": name, "state": "released", "at": at})
            # A pruned result is still a message of the chat, but its text is gone.
            if row["active"] and not _PRUNED.match(str(row["content"] or "")[:400]):
                item["state"] = "in"
    out = list(found.values())
    if len(_HISTORY_CACHE) >= 32:
        _HISTORY_CACHE.clear()
    _HISTORY_CACHE[key] = [dict(item) for item in out]
    return out


def session_family(session_id: str, *, db_path: Path | None = None, depth: int = 8) -> list[str]:
    """Other session ids of the same chat, nearest first: parents, then the newest line of children.

    Hermes gives a chat a new session id when it continues an ended one or
    compacts it; the new row names the old one in ``parent_session_id``. The
    desktop can keep using the old id while the agent already runs under the
    new one, so what was saved under one id has to be found from the other."""
    if not session_id:
        return []
    path = db_path or state_db_path()
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return []
    out: list[str] = []
    seen = {session_id}
    try:
        current = session_id
        for _ in range(depth):
            row = conn.execute("SELECT parent_session_id FROM sessions WHERE id = ?", (current,)).fetchone()
            parent = str(row[0]) if row and row[0] else ""
            if not parent or parent in seen:
                break
            seen.add(parent)
            out.append(parent)
            current = parent
        current = session_id
        for _ in range(depth):
            row = conn.execute(
                "SELECT id FROM sessions WHERE parent_session_id = ? ORDER BY started_at DESC LIMIT 1", (current,)
            ).fetchone()
            child = str(row[0]) if row and row[0] else ""
            if not child or child in seen:
                break
            seen.add(child)
            out.append(child)
            current = child
    except sqlite3.Error:
        return out
    finally:
        conn.close()
    return out


def project_context(cwd: str) -> str:
    """AGENTS.md-style files at the workspace root, clipped and joined."""
    if not cwd:
        return ""
    try:
        root = Path(cwd).expanduser()
    except (OSError, ValueError):
        return ""
    parts: list[str] = []
    for name in PROJECT_FILES:
        candidate = root / name
        try:
            if candidate.is_file():
                parts.append(_clip(candidate.read_text(encoding="utf-8", errors="replace"), _PROJECT_CLIP))
        except OSError:
            continue
    return "\n\n".join(parts)


def background_text(ctx: SessionContext, *, skip_latest_user: bool) -> str:
    """Everything except the focus message, newest material last."""
    users = ctx.users[:-1] if skip_latest_user and ctx.users else ctx.users
    pieces = [ctx.first_user, project_context(ctx.cwd)]
    pieces.extend(ctx.tools[-3:])
    pieces.extend(users[-3:])
    pieces.extend(ctx.assistants[-3:])
    return "\n\n".join(piece for piece in pieces if piece)


CONVERSATION_SLOTS = 2
# Conversation rankings only change when the chat does; keystrokes reuse them.
_CONVO_CACHE: dict = {}


def chip_ranking(
    judge,
    draft: str,
    session_id: str,
    *,
    limit: int = 5,
    db_path: Path | None = None,
    held: list[str] | None = None,
    exclude: list[str] | None = None,
    spare: int = 0,
) -> dict:
    """The desktop chips: what the user is typing plus what the chat is about.

    * Empty draft: the conversation ranking alone (newest user message as the
      focus, the rest as context), so the chips stay up between turns.
    * Draft present: the draft is cut into chunks (sentences, then ~10-word
      windows) and each chunk is ranked on its own, so "android crash ... and
      research on X" yields skills for both topics instead of one winner.
      Up to ``limit - 2`` chips come from the draft, the rest from the chat.
    * ``held`` skills (on the left pile, or loaded) add their descriptions to
      the context, so the chips can daisy-chain to skills that complement
      them. ``exclude`` names never come back, and the row refills past them.
    * A candidate that mostly repeats a held or loaded skill (name and
      description overlap >= ``REDUNDANT_SIMILARITY``) is dropped unless the
      draft supports it at ``REDUNDANT_KEEP`` or more.

    Each candidate carries ``source``: ``"draft"`` or ``"conversation"``.
    """
    draft = (draft or "").strip()
    excluded = {name.casefold() for name in (exclude or []) if name}
    held_skills = _held_skills(judge.store, list(held or []) + list(exclude or []))
    held_text = "\n".join(f"{skill.name} {skill.description}" for skill in held_skills)
    pool = min(40, limit + spare + len(excluded) + 4)
    ctx = session_context(session_id, db_path=db_path)
    convo = None
    if ctx.latest_user or ctx.first_user or ctx.cwd or held_text:
        key = (
            session_id,
            pool,
            ctx.cwd,
            len(ctx.users),
            len(ctx.assistants),
            len(ctx.tools),
            ctx.latest_user[-200:],
            (ctx.assistants[-1][-200:] if ctx.assistants else ""),
            held_text[-400:],
            getattr(judge.store, "db_path", None),
        )
        convo = _CONVO_CACHE.get(key)
        if convo is None:
            background = _join(background_text(ctx, skip_latest_user=True), held_text)
            convo = judge.rank(ctx.latest_user or held_text, context=background, limit=pool, independent=True)
            if len(_CONVO_CACHE) >= 32:
                _CONVO_CACHE.pop(next(iter(_CONVO_CACHE)))
            _CONVO_CACHE[key] = convo
    convo_items = [item for item in (convo.candidates if convo else []) if _keep(item, excluded, held_skills)]
    if not draft:
        rows = [_row(item, "conversation", judge.store, draft) for item in convo_items[:limit]]
        extra = [_row(item, "conversation", judge.store, draft) for item in convo_items[limit:limit + spare]]
        return {"candidates": rows, "spare": extra, "none_probability": convo.none_probability if convo else 1.0}

    background = _join(background_text(ctx, skip_latest_user=False), held_text)
    best: dict[str, tuple] = {}
    held_keys = {str(name).casefold() for name in (held or [])}
    held_scores: dict[str, float] = {}
    none_probability = 1.0
    for chunk in draft_chunks(draft):
        ranking = _chunk_rank(judge, chunk, background, pool)
        none_probability = min(none_probability, ranking.none_probability)
        for item in ranking.candidates:
            key = item.name.casefold()
            if key in held_keys:
                # Held skills are not chips, but auto-attach needs to know
                # whether the draft still supports them.
                held_scores[item.name] = max(held_scores.get(item.name, 0.0), item.probability)
            if not _keep(item, excluded, held_skills):
                continue
            if key not in best or item.probability > best[key][0].probability:
                best[key] = (item, _context_only(item.why))
    ordered = sorted(best.values(), key=lambda pair: -pair[0].probability)
    from_draft = [item for item, context_only in ordered if not context_only]
    from_context = [item for item, context_only in ordered if context_only]
    typed_slots = max(1, limit - min(CONVERSATION_SLOTS, len(convo_items)))
    picked: list[dict] = []
    seen: set[str] = set()

    def take(items, source, room):
        for item in items:
            if len(picked) >= room:
                return
            name = item.name.casefold()
            if name in seen:
                continue
            seen.add(name)
            picked.append(_row(item, source, judge.store, draft))

    take(from_draft, "draft", typed_slots)
    take(convo_items + from_context, "conversation", limit)
    take(from_draft, "draft", limit)
    # Next-best chips the desktop keeps in hand, so a pick is refilled at once
    # without waiting for a new ranking.
    shown = len(picked)
    take(from_draft, "draft", limit + spare)
    take(convo_items + from_context, "conversation", limit + spare)
    extra, picked = picked[shown:], picked[:shown]
    return {"spare": extra,
            "candidates": picked, "none_probability": none_probability, "held_scores": held_scores}


# A candidate this similar to a held/loaded skill repeats it; it only shows
# when the draft itself supports it this strongly.
REDUNDANT_SIMILARITY = 0.7
REDUNDANT_KEEP = 0.95
_CHUNK_WORDS = 10
_MAX_CHUNKS = 6
_SENTENCE = re.compile(r"(?<=[.!?;])\s+|\n+")


# (catalog, chunk, background, pool) -> Ranking. While the user types only the
# last chunk changes, so the earlier ones are free.
_CHUNK_CACHE: dict = {}


def _chunk_rank(judge, chunk: str, background: str, pool: int):
    signature = getattr(judge.store, "catalog_signature", lambda: ())()
    key = (str(getattr(judge.store, "db_path", "")), signature, chunk, hash(background), pool)
    hit = _CHUNK_CACHE.get(key)
    if hit is not None:
        return hit
    ranking = judge.rank(chunk, context=background, limit=pool, independent=True)
    if len(_CHUNK_CACHE) >= 256:
        _CHUNK_CACHE.pop(next(iter(_CHUNK_CACHE)))
    _CHUNK_CACHE[key] = ranking
    return ranking


def draft_chunks(draft: str) -> list[str]:
    """Sentences of the draft, long ones cut into ~10-word windows (max 6 chunks).

    A one-sentence draft of up to 10 words is a single chunk."""
    chunks: list[str] = []
    for sentence in _SENTENCE.split(draft or ""):
        words = sentence.split()
        if not words:
            continue
        for start in range(0, len(words), _CHUNK_WORDS):
            piece = words[start : start + _CHUNK_WORDS]
            if chunks and len(piece) < 3:
                chunks[-1] = f"{chunks[-1]} {' '.join(piece)}"
            else:
                chunks.append(" ".join(piece))
    if len(chunks) > _MAX_CHUNKS:
        chunks = chunks[: _MAX_CHUNKS - 1] + [" ".join(chunks[_MAX_CHUNKS - 1 :])]
    return chunks or ([draft] if draft else [])


def skill_similarity(left, right) -> float:
    """Overlap of two skills' name and description words (Jaccard, 0..1)."""
    a, b = _skill_words(left), _skill_words(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _skill_words(skill) -> set[str]:
    text = f"{getattr(skill, 'name', '')} {getattr(skill, 'description', '')}".lower()
    return {word for word in re.findall(r"[a-z0-9]+", text) if len(word) > 2}


def _keep(item, excluded: set[str], held_skills: list) -> bool:
    if item.name.casefold() in excluded:
        return False
    if item.probability >= REDUNDANT_KEEP:
        return True
    return all(skill_similarity(item, skill) < REDUNDANT_SIMILARITY for skill in held_skills)


def _held_skills(store, names: list[str]) -> list:
    out = []
    seen: set[str] = set()
    for name in names:
        key = str(name or "").casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        skill = store.get(str(name))
        if skill is not None:
            out.append(skill)
    return out


def _join(*parts: str) -> str:
    return "\n\n".join(part for part in parts if part)


def _context_only(why: str) -> bool:
    """True when nothing in the draft supported this candidate."""
    parts = [part.strip() for part in (why or "").split(";") if part.strip()]
    return bool(parts) and all(part.startswith("context") for part in parts)


def _row(item, source: str, store, draft: str) -> dict:
    skill = store.get(item.name)
    return {
        "name": item.name,
        "description": item.description,
        "probability": item.probability,
        "why": item.why,
        "label": item.label(),
        "source": source,
        "anchored": skill is not None and auto_evidence(store, draft, skill),
    }


# A skill may attach itself only on evidence in what the user wrote as prose.
# Paths, attachment references, code and URLs name things without asking for them.
_NOT_PROSE = re.compile(
    r"```.*?(?:```|$)"
    r"|`[^`]*`"
    r"|@(?:file|folder|image|url):(?:`[^`]*`|\S+)"
    r"|https?://\S+"
    r"|\S+[\\/]\S*",
    re.S,
)
# Sum of ln(skills / skills holding the word) over the matched words. 3.0 means
# one word found in at most about 5% of the skills is enough on its own, while
# "go", "build" or "issues" are not. Tuned with `python run.py eval`.
AUTO_EVIDENCE = 3.0
# Below this many skills rarity cannot be measured; any prose hit counts.
_RARITY_MIN_SKILLS = 200
# (catalog path, catalog signature) -> {word: skills holding it}
_DOC_FREQ: dict = {}


def prose_only(text: str) -> str:
    return _NOT_PROSE.sub(" ", text or "")


def auto_evidence(store, text: str, skill) -> bool:
    """True when the prose of ``text`` supports loading ``skill`` unasked.

    Either the full name of a skill with a name of two or more words, or
    words of its name or description that are rare enough in the catalog."""
    prose = prose_only(text)
    name_words, desc_words, _body, _doc = _skill_text(skill)
    phrase = skill.name.casefold().replace("_", "-")
    if len(name_words) >= 2 and re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", prose.casefold()):
        return True
    hits = set(tokenize(prose)) & (name_words | desc_words)
    if not hits:
        return False
    total = store.count()
    if total < _RARITY_MIN_SKILLS:
        return True
    key = (str(getattr(store, "db_path", "")), store.catalog_signature())
    known = _DOC_FREQ.get(key)
    if known is None:
        _DOC_FREQ.clear()
        known = _DOC_FREQ[key] = {}
    evidence = 0.0
    for word in hits:
        if word not in known:
            known[word] = store.doc_frequency(word)
        evidence += math.log(total / max(1, known[word]))
    return evidence >= AUTO_EVIDENCE


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit]


def _parts_any_order(term: str, parts: list[str]) -> bool:
    """Every typed part starts a different part of the name, in any order."""
    wanted = [piece for piece in re.split(r"[-_\s]+", term) if piece]
    if len(wanted) < 2 or len(wanted) > len(parts):
        return False
    free = list(parts)
    for piece in wanted:
        hit = next((part for part in free if part.startswith(piece)), None)
        if hit is None:
            return False
        free.remove(hit)
    return True


def name_search(store, term: str, *, limit: int = 8, exclude: list[str] | None = None) -> list[dict]:
    """Skills for a typed ``/term``: whole name, name start, a name part, inside
    the name, then typo-tolerant, then description/body. Fast (names are cached)."""
    import difflib

    term = (term or "").strip().lower()
    if term.startswith("triage:"):
        term = term[len("triage:"):]
    term = term.lstrip("/")
    if not term:
        return []
    skip = {name.casefold() for name in (exclude or [])}
    scored: dict[str, tuple[float, str, object]] = {}

    def offer(skill, score: float, why: str) -> None:
        key = skill.name.casefold()
        if key in skip:
            return
        if key not in scored or scored[key][0] < score:
            scored[key] = (score, why, skill)

    skills = store.list_skills()
    for skill in skills:
        name = skill.name.lower()
        parts = re.split(r"[-_]", name)
        if name == term:
            offer(skill, 1.0, "name")
        elif name.startswith(term):
            offer(skill, 0.95 - min(0.2, (len(name) - len(term)) * 0.005), "name starts")
        elif any(part.startswith(term) for part in parts):
            offer(skill, 0.85, "name part")
        elif _parts_any_order(term, parts):
            offer(skill, 0.8, "name parts, any order")
        elif term in name:
            offer(skill, 0.75, "in name")
    if len(scored) < limit and len(term) >= 3:
        for skill in skills:
            name = skill.name.lower()
            ratio = max(
                [difflib.SequenceMatcher(None, term, name).ratio()]
                + [difflib.SequenceMatcher(None, term, part).ratio() for part in re.split(r"[-_]", name)]
            )
            if ratio >= 0.75:
                offer(skill, 0.7 * ratio, "close to name")
    if len(scored) < limit:
        strengths = store.fts_bm25(term, limit=limit * 2) or {}
        best = max(strengths.values(), default=0.0) or 1.0
        for name, value in strengths.items():
            skill = store.get(name)
            if skill is not None:
                offer(skill, 0.5 * (value / best), "description or body")
    ordered = sorted(scored.values(), key=lambda item: (-item[0], item[2].name))[:limit]
    return [
        {
            "name": skill.name,
            "description": skill.description,
            "probability": round(score, 4),
            "why": why,
            "source": "search",
            "anchored": True,
        }
        for score, why, skill in ordered
    ]
