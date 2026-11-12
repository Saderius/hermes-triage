"""Turn orchestration: rank, shortlist, inject one body, remember the approval."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from .catalog_io import HOOK_CONTEXT_SAFE_CHARS, activation_text
from .catalog_link import invoked_skills, leading_tokens, parse_folders
from .context import project_context, strip_injected
from .preload import merge_names, parse_names, project_names
from .guard import evaluate_skill_view, normalize_skill_name, skill_name_from_args
from .judge import Ranking, SkillJudge, load_embedder
from .store import Skill, SkillStore

logger = logging.getLogger("triage")

_SKIP = frozenset(
    {
        "skip",
        "none",
        "nothing",
        "no",
        "no skill",
        "no thanks",
        "cancel",
    }
)
_NONINTERACTIVE_PLATFORMS = frozenset({"cron", "subagent", "api", "batch"})


@dataclass
class Settings:
    auto_accept_threshold: float = 0.90
    shortlist_size: int = 5
    strip_skills_index: bool = True
    interactive: bool = True
    embedder: str = ""
    always_load: tuple[str, ...] = ()
    skill_folders: tuple[str, ...] = ()
    register_slash: bool = False
    max_attached: int = 0  # 0 = no limit
    model_tools: bool = True
    hide_hermes_skills: bool = True

    @classmethod
    def from_mapping(cls, values: dict | None) -> "Settings":
        values = values or {}
        return cls(
            auto_accept_threshold=_clamp_threshold(values.get("auto_accept_threshold", 0.90)),
            shortlist_size=_clamp_size(values.get("shortlist_size", 5)),
            strip_skills_index=_as_bool(values.get("strip_skills_index", True), True),
            interactive=_as_bool(values.get("interactive", True), True),
            embedder=str(values.get("embedder") or ""),
            always_load=tuple(parse_names(values.get("always_load"))),
            skill_folders=tuple(parse_folders(values.get("skill_folders"))),
            register_slash=_as_bool(values.get("register_slash", False), False),
            max_attached=_clamp_attached(values.get("max_attached", 0)),
            model_tools=_as_bool(values.get("model_tools", True), True),
            hide_hermes_skills=_as_bool(values.get("hide_hermes_skills", True), True),
        )


def settings_from_ctx(ctx) -> Settings:
    def one(key: str, default):
        try:
            value = ctx.get_config(key, default)
        except Exception:
            return default
        return default if value is None else value

    return Settings.from_mapping(
        {
            "auto_accept_threshold": one("auto_accept_threshold", 0.90),
            "shortlist_size": one("shortlist_size", 5),
            "strip_skills_index": one("strip_skills_index", True),
            "interactive": one("interactive", True),
            "embedder": one("embedder", ""),
            "always_load": one("always_load", ""),
            "skill_folders": one("skill_folders", ""),
            "register_slash": one("register_slash", False),
            "max_attached": one("max_attached", 0),
            "model_tools": one("model_tools", True),
            "hide_hermes_skills": one("hide_hermes_skills", True),
        }
    )


class ApprovalLedger:
    """Names the user accepted.

    A loaded skill stays approved for the rest of that session, so a later
    turn can ``skill_view`` it and its linked files. Names that were never
    accepted stay blocked. A call with no session id is still turn-scoped.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_scope: dict[str, set[str]] = {}
        self._by_session: dict[str, set[str]] = {}

    def approve(self, scope: str, names: list[str], session_id: str = "") -> None:
        normalized = {normalize_skill_name(name) for name in names if name}
        with self._lock:
            self._by_scope.setdefault(scope, set()).update(normalized)
            if session_id:
                self._by_session.setdefault(session_id, set()).update(normalized)

    def allows(self, scope: str, name: str, session_id: str = "") -> bool:
        normalized = normalize_skill_name(name)
        if not normalized:
            return False
        with self._lock:
            if session_id and normalized in self._by_session.get(session_id, set()):
                return True
            return normalized in self._by_scope.get(scope, set())

    def session_names(self, session_id: str) -> list[str]:
        with self._lock:
            names = self._by_session.get(session_id, set())
            return sorted(names)

    def copy_session(self, old_session_id: str, session_id: str) -> None:
        if not old_session_id or not session_id or old_session_id == session_id:
            return
        with self._lock:
            carried = set(self._by_session.get(old_session_id, set()))
            if carried:
                self._by_session.setdefault(session_id, set()).update(carried)


@dataclass
class _Pending:
    ranking: Ranking


@dataclass
class _Forced:
    name: str
    probability: float
    why: str


class SessionMemory:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._shortlists: dict[str, _Pending] = {}
        self._forced: dict[str, _Forced] = {}
        self._bridge_pending: set[str] = set()

    def remember(self, session_id: str, ranking: Ranking) -> None:
        with self._lock:
            self._shortlists[session_id] = _Pending(ranking)
            self._trim(self._shortlists)

    def shortlist(self, session_id: str) -> Ranking | None:
        with self._lock:
            pending = self._shortlists.get(session_id)
        return pending.ranking if pending else None

    def clear_shortlist(self, session_id: str) -> None:
        with self._lock:
            self._shortlists.pop(session_id, None)

    def force(self, session_id: str, name: str, probability: float, why: str) -> None:
        with self._lock:
            self._forced[session_id] = _Forced(name, probability, why)
            self._trim(self._forced)

    def pop_forced(self, session_id: str) -> _Forced | None:
        with self._lock:
            return self._forced.pop(session_id, None)

    def note_compaction(self, session_id: str) -> None:
        """Drop pending chips and one-shot body injections. Bridges stay approved."""
        with self._lock:
            self._shortlists.pop(session_id, None)
            self._forced.pop(session_id, None)
            if session_id:
                self._bridge_pending.add(session_id)

    def take_bridge(self, session_id: str) -> bool:
        with self._lock:
            if session_id not in self._bridge_pending:
                return False
            self._bridge_pending.remove(session_id)
            return True

    def transfer(self, old_session_id: str, session_id: str) -> None:
        if not old_session_id or not session_id or old_session_id == session_id:
            return
        with self._lock:
            if old_session_id in self._shortlists:
                self._shortlists[session_id] = self._shortlists.pop(old_session_id)
            if old_session_id in self._forced:
                self._forced[session_id] = self._forced.pop(old_session_id)
            if old_session_id in self._bridge_pending:
                self._bridge_pending.discard(old_session_id)
                self._bridge_pending.add(session_id)

    @staticmethod
    def _trim(bucket: dict) -> None:
        if len(bucket) > 32:
            for key in list(bucket)[: len(bucket) - 32]:
                bucket.pop(key, None)


class Runtime:
    def __init__(self, store: SkillStore, ctx=None, settings: Settings | None = None, ask_choice=None):
        self.store = store
        self.ctx = ctx
        self._settings = settings or Settings()
        self._ask_override = ask_choice
        self.judge = SkillJudge(store)
        self._embedder_path = ""
        self.ledger = ApprovalLedger()
        self.memory = SessionMemory()
        # session -> casefolded names whose full body is already in the chat
        # (inlined with the user's message). skill_view for them returns a short
        # note instead of the body again; compaction clears it.
        self._inlined: dict[str, set[str]] = {}
        self._preload_lock = threading.Lock()
        self._preloaded: set[str] = set()
        self._recompacted: set[str] = set()

    def settings(self) -> Settings:
        if self.ctx is not None:
            return settings_from_ctx(self.ctx)
        return self._settings

    def on_pre_llm(self, **kwargs):
        """Always-load skills (first prompt, and first prompt after a compaction),
        then the judge's decision for this turn."""
        preload = self._always_load_context(kwargs)
        result = self._decide(**kwargs, triage_budget=max(0, HOOK_CONTEXT_SAFE_CHARS - len(preload or "")))
        if not preload:
            return result
        if not result:
            return {"context": preload}
        merged = dict(result)
        merged["context"] = _join_context(preload, result.get("context"))
        return merged

    def always_load_names(self, cwd: str = "") -> tuple[list[str], str]:
        """Global names then project names, deduped. Returns (names, project file or '')."""
        project, path = project_names(cwd)
        return merge_names(self.settings().always_load, project), (str(path) if path else "")

    def _attached_skills(
        self,
        text: str,
        turn_id: str,
        session_id: str,
        scope: str,
        pending: list[str] | None = None,
        *,
        budget: int = HOOK_CONTEXT_SAFE_CHARS,
    ) -> str | None:
        """Skills the user attached to this message (chip click, auto-attach, or typed ``/triage:name``).

        Returns ``None`` when the message carries none. Otherwise the skills are approved and
        recorded for the fan, and the return value is the context to add: empty when Hermes
        already expanded the tokens into skill blocks, the bodies when it left them as text.
        The judge adds no shortlist to such a turn."""
        expanded = [name for name in invoked_skills(text) if self._skill_by_normalized(name) is not None]
        if expanded:
            expanded_skills = [self._skill_by_normalized(name) for name in expanded]
            typed: list = []
        else:
            expanded_skills = []
            names, _rest = leading_tokens(text, self._catalog_name)
            typed = [self._skill_by_normalized(name) for name in names]
        # Skills held on the desktop's left pile travel outside the prompt text.
        held = [self._skill_by_normalized(name) for name in (pending or [])]
        limit = self.settings().max_attached or 10**9
        skills: list = []
        to_inject: list = []
        seen: set[str] = set()
        for skill, inject in [(s, False) for s in expanded_skills] + [(s, True) for s in typed + held]:
            if skill is None or skill.name.casefold() in seen or len(skills) >= max(limit, len(expanded_skills)):
                continue
            seen.add(skill.name.casefold())
            skills.append(skill)
            if inject:
                to_inject.append(skill)
        if not skills:
            return None
        self.ledger.approve(scope, [skill.name for skill in skills], session_id)
        for skill in skills:
            self.store.bump_use(skill.name)
            self._log(turn_id, session_id, text[:500], [], skill.name, True, None, "attached")
        self._note_body_injected(session_id)
        if not to_inject:
            return ""
        text = render_always_load(
            to_inject,
            [],
            header=(
                "[triage] Skills attached to this message. Follow them for this turn. "
                "Their full text is below; do not call skill_view for them "
                "(only for linked files, or for skills listed as too large)."
            ),
            budget=budget,
        )
        inlined = {skill.name.casefold() for skill in to_inject if f"# {skill.name}\n" in text}
        if inlined:
            self._inlined.setdefault(session_id, set()).update(inlined)
        return text

    def _render_loaded(self, session_id: str, skill: Skill, probability: float | None, why: str) -> str:
        """render_loaded, and remember the skill as in the chat when its body went inline."""
        text = render_loaded(skill, probability, why)
        if skill.body and skill.body.strip()[:120] in text:
            self._inlined.setdefault(session_id, set()).add(skill.name.casefold())
        return text

    def _catalog_name(self, name: str) -> str | None:
        skill = self._skill_by_normalized(name)
        return skill.name if skill is not None else None

    def _background(self, kwargs: dict, history) -> str:
        """Workspace AGENTS.md/README plus the session's first user message.

        Recent turns already reach the judge through ``conversation_history``;
        this adds what the session started from, which drops out of the short
        history window once the chat grows."""
        try:
            parts = [project_context(self._session_cwd(kwargs))]
            from .context import own_words

            for message in history or []:
                if isinstance(message, dict) and message.get("role") == "user":
                    # The first message with words of its own: after a compression
                    # the first one is Hermes' note, and notes are not the topic.
                    text = own_words(strip_injected(message_text(message.get("content"))))
                    if text:
                        parts.append(text[:4000])
                        break
            return "\n\n".join(part for part in parts if part)
        except Exception:
            logger.exception("triage: background context failed")
            return ""

    def _session_cwd(self, kwargs: dict) -> str:
        for key in ("cwd", "session_cwd", "workspace"):
            value = kwargs.get(key)
            if value:
                return str(value)
        ctx = self.ctx
        if ctx is not None and hasattr(ctx, "session_cwd"):
            try:
                return str(ctx.session_cwd() or "")
            except Exception:
                return ""
        return ""

    def _always_load_context(self, kwargs: dict) -> str:
        session_id = str(kwargs.get("session_id") or "")
        key = session_id or "__no_session__"
        first = bool(kwargs.get("is_first_turn")) or not kwargs.get("conversation_history")
        with self._preload_lock:
            if key in self._preloaded:
                return ""
            if not first and key not in self._recompacted:
                return ""
            self._preloaded.add(key)
            self._recompacted.discard(key)
        try:
            names, project_file = self.always_load_names(self._session_cwd(kwargs))
        except Exception:
            logger.exception("triage: always-load list could not be read")
            return ""
        if not names:
            return ""
        found = []
        missing = []
        for name in names:
            skill = self._skill_by_normalized(name)
            (found if skill is not None else missing).append(skill if skill is not None else name)
        turn_id = str(kwargs.get("turn_id") or "")
        scope = scope_key(turn_id, session_id)
        if found:
            self.ledger.approve(scope, [skill.name for skill in found], session_id)
            for skill in found:
                self._log(turn_id, session_id, "[always-load]", [], skill.name, True, None, "always-load")
            self._note_body_injected(session_id)
        text = render_always_load(found, missing, project_file)
        inlined = {skill.name.casefold() for skill in found if f"# {skill.name}\n" in text}
        if inlined:
            self._inlined.setdefault(session_id, set()).update(inlined)
        return text

    def _decide(self, **kwargs):
        settings = self.settings()
        self._ensure_embedder(settings)
        text = message_text(kwargs.get("user_message"))
        session_id = str(kwargs.get("session_id") or "")
        turn_id = str(kwargs.get("turn_id") or "")
        platform = str(kwargs.get("platform") or "")
        scope = scope_key(turn_id, session_id)
        bridge = self._consume_bridge_stub(session_id)
        if not text.strip():
            if kwargs.get("is_first_turn") and self.store.count() == 0:
                return {"context": _join_context(bridge, _empty_catalog_note())}
            return {"context": bridge} if bridge else None

        # The first-turn guesses (newest fresh pile, any recent chip activity) are
        # for a desktop chat that has no stored id yet. Telegram, CLI and cron
        # turns name their platform and never take them.
        chip_first_turn = bool(kwargs.get("is_first_turn")) and (platform in _CHIP_PLATFORMS or not platform)
        pending = self.store.pop_pending_attach(session_id, first_turn=chip_first_turn)
        # Hermes may have given this chat a new id since the pile was saved
        # (a continued or compacted chat); the pile is then under the old id.
        from .context import session_family

        family = session_family(session_id)
        if not pending:
            for other in family:
                pending = self.store.pop_pending_attach(other)
                if pending:
                    break
        for other in family:
            self.store.set_pending_attach(other, [])
        budget = kwargs.get("triage_budget")
        budget = HOOK_CONTEXT_SAFE_CHARS if budget is None else int(budget)
        attached = self._attached_skills(
            text, turn_id, session_id, scope, pending, budget=budget
        )
        if attached is not None:
            self.memory.clear_shortlist(session_id)
            return {"context": _join_context(bridge, attached)} if (bridge or attached) else None

        forced = self._pop_force(session_id)
        if forced is not None:
            skill = self.store.get(forced.name)
            if skill is not None:
                self.ledger.approve(scope, [skill.name], session_id)
                self.store.bump_use(skill.name)
                self._log(turn_id, session_id, text, [], skill.name, True, forced.probability, forced.why)
                self._note_body_injected(session_id)
                return {"context": _join_context(bridge, self._render_loaded(session_id, skill, forced.probability, forced.why))}

        if platform in _CHIP_PLATFORMS or self.store.chip_session_active(session_id, first_turn=chip_first_turn):
            # Desktop: the magazine is the only way a skill loads. The server
            # neither auto-loads nor puts a "pick one" list on the model; a
            # skill the user took out of the magazine stays out.
            return {"context": bridge} if bridge else None

        pending = self.memory.shortlist(session_id)
        pick = parse_pick(text, pending) if pending is not None else None
        if pick == "__skip__":
            self.memory.clear_shortlist(session_id)
            self._log(turn_id, session_id, text, pending.as_log() if pending else [], None, False, None, "skip")
            return {
                "context": _join_context(
                    bridge,
                    "[triage] No skill loaded for this turn. Do not call skill_view.",
                )
            }
        if isinstance(pick, str):
            skill = self.store.get(pick)
            if skill is not None:
                probability, why = _pending_meta(pending, skill.name)
                self.memory.clear_shortlist(session_id)
                self.ledger.approve(scope, [skill.name], session_id)
                self.store.bump_use(skill.name)
                self._log(turn_id, session_id, text, pending.as_log() if pending else [], skill.name, True, probability, why)
                self._note_body_injected(session_id)
                return {"context": _join_context(bridge, self._render_loaded(session_id, skill, probability, why))}

        history = kwargs.get("conversation_history")
        background = self._background(kwargs, history)
        ranking = self.judge.rank(
            text,
            limit=settings.shortlist_size,
            history=history,
            thoughts=_thought_text(kwargs),
            context=background,
        )
        if not ranking.candidates:
            if kwargs.get("is_first_turn") and self.store.count() == 0:
                return {"context": _join_context(bridge, _empty_catalog_note())}
            return {"context": bridge} if bridge else None

        top = ranking.candidates[0]
        # Auto-accept is decided on the message itself. Background (AGENTS.md,
        # the first prompt) may reorder the shortlist but never loads a skill.
        strict_top = top
        if background:
            strict = self.judge.rank(
                text, limit=settings.shortlist_size, history=history, thoughts=_thought_text(kwargs)
            )
            strict_top = strict.candidates[0] if strict.candidates else None
        if (
            settings.auto_accept_threshold > 0
            and strict_top is not None
            and strict_top.name == top.name
            and strict_top.probability >= settings.auto_accept_threshold
            and _auto_evidence(self.store, text, strict_top.name)
        ):
            skill = self.store.get(top.name)
            if skill is None:
                return None
            self.memory.clear_shortlist(session_id)
            self.ledger.approve(scope, [skill.name], session_id)
            self.store.bump_use(skill.name)
            self._log(turn_id, session_id, text, ranking.as_log(), skill.name, True, top.probability, top.why)
            self._note_body_injected(session_id)
            return {"context": _join_context(bridge, self._render_loaded(session_id, skill, top.probability, top.why + "; auto-accepted"))}

        asked = self._maybe_ask(settings, platform, ranking)
        if asked == "__skip__":
            self._log(turn_id, session_id, text, ranking.as_log(), None, False, None, "skip")
            return {
                "context": _join_context(
                    bridge,
                    "[triage] No skill loaded for this turn. Do not call skill_view.",
                )
            }
        if asked:
            skill = self.store.get(asked)
            if skill is not None:
                meta = next((item for item in ranking.candidates if item.name == skill.name), top)
                self.ledger.approve(scope, [skill.name], session_id)
                self.store.bump_use(skill.name)
                self._log(
                    turn_id, session_id, text, ranking.as_log(), skill.name, True, meta.probability, meta.why
                )
                self._note_body_injected(session_id)
                return {"context": _join_context(bridge, self._render_loaded(session_id, skill, meta.probability, meta.why))}

        self.memory.remember(session_id, ranking)
        self._log(turn_id, session_id, text, ranking.as_log(), None, False, None, "shortlist")
        return {"context": _join_context(bridge, render_shortlist(ranking, self._unlocked_labels(session_id)))}

    def on_post_compaction(self, **kwargs) -> None:
        """Keep session bridges after Hermes drops compacted skill bodies.

        ``post_compaction`` is an observer: its return value is ignored.
        Hermes documents that plugins re-inject from the next ``pre_llm_call``.
        """
        session_id = str(kwargs.get("session_id") or "")
        old_session_id = str(kwargs.get("old_session_id") or "")
        if old_session_id and old_session_id != session_id:
            self.ledger.copy_session(old_session_id, session_id)
            self.memory.transfer(old_session_id, session_id)
        self.memory.note_compaction(session_id)
        self._inlined.pop(session_id, None)
        if old_session_id:
            self._inlined.pop(old_session_id, None)
        with self._preload_lock:
            # The always-load bodies left with the compacted turns: re-apply on the next prompt.
            self._preloaded.discard(session_id or "__no_session__")
            self._recompacted.add(session_id or "__no_session__")
        if old_session_id and old_session_id != session_id:
            self.store.clear_pending_forces(old_session_id)
        self.store.clear_pending_forces(session_id)
        self.store.mark_session_compacted(session_id)
        if old_session_id and old_session_id != session_id:
            self.store.link_session(session_id, old_session_id)

    def unlocked_stub(self, session_id: str) -> str | None:
        names = self.ledger.session_names(session_id)
        if not names:
            return None
        lines = [
            "[triage] Skills unlocked in this chat.",
            "Their bodies were compacted out of the window. The bridges remain:",
            "call skill_view(name) to load one again. Full skill text is not inlined here.",
        ]
        for name in names:
            skill = self._skill_by_normalized(name)
            if skill is None:
                lines.append(f"- {name}")
                continue
            description = " ".join(skill.description.split())
            if len(description) > 140:
                description = description[:139].rstrip() + "…"
            lines.append(f"- {skill.name}: {description}" if description else f"- {skill.name}")
        return "\n".join(lines)

    def _pop_force(self, session_id: str) -> _Forced | None:
        """In-memory slash pick, else a durable desktop approval. One turn consumes both."""
        memorized = self.memory.pop_forced(session_id)
        durable = self.store.pop_pending_force(session_id)
        if memorized is not None:
            return memorized
        if durable is None:
            return None
        return _Forced(durable["name"], durable["probability"], durable["why"])

    def _consume_bridge_stub(self, session_id: str) -> str | None:
        if not self.memory.take_bridge(session_id):
            return None
        return self.unlocked_stub(session_id)

    def _note_body_injected(self, session_id: str) -> None:
        self.store.clear_session_compacted(session_id)

    def _unlocked_labels(self, session_id: str) -> list[str]:
        labels: list[str] = []
        for name in self.ledger.session_names(session_id):
            skill = self._skill_by_normalized(name)
            labels.append(skill.name if skill is not None else name)
        return labels

    def _skill_by_normalized(self, name: str):
        direct = self.store.get(name)
        if direct is not None:
            return direct
        for skill in self.store.list_skills():
            if skill.name.casefold() == name.casefold():
                return skill
        return None

    def on_pre_tool(self, **kwargs):
        session_id = str(kwargs.get("session_id") or "")
        scope = scope_key(str(kwargs.get("turn_id") or ""), session_id)
        return evaluate_skill_view(
            str(kwargs.get("tool_name") or ""),
            kwargs.get("args"),
            is_approved=lambda name: self.ledger.allows(scope, name, session_id),
        )

    def skill_is_approved(self, name: str, *, turn_id: str = "", session_id: str = "") -> bool:
        return self.ledger.allows(scope_key(turn_id, session_id), name, session_id)

    def serve_skill_view(self, args: dict, *, turn_id: str = "", session_id: str = "") -> str | None:
        """Full skill_view JSON for an approved catalog skill, or None to let Hermes try."""
        from .catalog_io import serve_skill_view

        parsed = _args(args)
        name = skill_name_from_args(parsed)
        if not name or not self.skill_is_approved(name, turn_id=turn_id, session_id=session_id):
            return None
        skill = self.store.get(name) or self.store.get(name.split(":")[-1].strip())
        if skill is None:
            return None
        file_path = parsed.get("file_path")
        if not file_path and skill.name.casefold() in self._inlined.get(session_id, set()):
            return already_loaded_view(skill)
        if not file_path:
            # The model read the whole skill: on the fan it counts as loaded
            # (again, when it had been released by a compaction).
            self._log(turn_id, session_id, "skill_view", [], skill.name, True, None, "skill_view")
            self._inlined.setdefault(session_id, set()).add(skill.name.casefold())
        return serve_skill_view(skill, file_path if isinstance(file_path, str) else None)

    def serve_skill_manage(self, args: dict, *, session_id: str = "") -> str | None:
        """skill_manage on catalog skills, as a JSON string; ``None`` hands the call to Hermes.

        A skill the model creates (or any name Triage does not index) belongs to
        Hermes: the call goes through to Hermes' own skill_manage and lands in
        Hermes' skills folder. Triage only marks it as allowed in this chat (see
        ``note_hermes_manage``); the folder watcher may index it later, in place.
        Edits to catalog skills are applied where those skills live."""
        import json

        from .catalog_io import apply_skill_manage

        parsed = _args(args)
        skills = [self.store.get(name) for name in _manage_names(parsed)]
        if not any(skill is not None for skill in skills):
            return None
        # Hermes' own skills are Hermes' to edit: its skill_manage runs, with its
        # guard scan (skills.guard_agent_created) when the user turned that on.
        roots = _hermes_skill_roots()
        if any(skill is not None and _under_any(skill.source, roots) for skill in skills):
            return None
        action = str(parsed.get("action") or "").strip().lower()
        if _guard_agent_created() and _writes(parsed):
            return json.dumps({
                "success": False,
                "error": (
                    "triage: skills.guard_agent_created is on, so model edits to skills are scanned by Hermes. "
                    "Triage does not edit catalog skills while that is on; use Hermes' own skill_manage on a skill "
                    "in Hermes' skills folder, or ask the user to make the change."
                ),
                "served_by": "triage",
            })
        operations = parsed.get("operations") if isinstance(parsed.get("operations"), list) else None
        if operations is None and action not in {"create", ""} and not self._manage_names_approved(parsed, session_id):
            return json.dumps({
                "success": False,
                "error": (
                    "triage: skill_manage is closed until this session loads the skill. "
                    "Pick it from the shortlist or create a new one with action='create'."
                ),
                "served_by": "triage",
            })
        if operations is not None and not self._batch_approved(operations, session_id):
            return json.dumps({
                "success": False,
                "error": "triage: every skill_manage operation must target a skill this session has loaded, or be a create.",
                "served_by": "triage",
            })
        result = apply_skill_manage(self.store, parsed)
        if result.get("success"):
            self._approve_managed(parsed, result, session_id)
        return json.dumps(result, ensure_ascii=False)

    def note_hermes_manage(self, args: dict, session_id: str = "") -> None:
        """Hermes handled a skill_manage call: the skill it made is the model's own,
        so this chat may view and edit it."""
        names = _manage_names(_args(args))
        if names:
            self.ledger.approve(scope_key("", session_id), names, session_id)

    def _manage_names_approved(self, args: dict, session_id: str) -> bool:
        name = str(args.get("name") or "")
        return bool(name) and self.skill_is_approved(name, session_id=session_id)

    def _batch_approved(self, operations: list, session_id: str) -> bool:
        for op in operations:
            if not isinstance(op, dict):
                return False
            action = str(op.get("action") or "").strip().lower()
            if action == "create":
                continue
            if not self._manage_names_approved(op, session_id):
                return False
        return True

    def _approve_managed(self, args: dict, result: dict, session_id: str) -> None:
        names: list[str] = []
        if isinstance(args.get("operations"), list):
            for op in args["operations"]:
                if isinstance(op, dict) and op.get("name"):
                    names.append(str(op["name"]))
        elif args.get("name"):
            names.append(str(args["name"]))
        if names:
            self.ledger.approve(scope_key("", session_id), names, session_id)

    def handle_command(self, raw_args: str, *, session_id: str = "") -> str:
        settings = self.settings()
        self._ensure_embedder(settings)
        text = (raw_args or "").strip()
        if not text or text in {"help", "-h", "--help"}:
            return _command_help()
        if text == "list":
            skills = self.store.list_skills()
            if not skills:
                return "Skill store is empty. Run: python run.py migrate --from <skills-dir>"
            lines = [f"{skill.name} — {skill.description}" for skill in skills]
            return "\n".join(lines)
        if text == "sync":
            folders = list(settings.skill_folders)
            if not folders:
                return "No skill folders configured. Add them under Settings → Plugins → triage → Skill folders."
            from .catalog_link import sync_folders
            from .watcher import reconcile_all

            lines = sync_folders(self.store, folders)
            lines += reconcile_all(self.store, folders) or ["No skill folders were added or removed."]
            return "\n".join(lines + ["Folders are also watched: changes show up within 30 s."])
        if text == "folders":
            from .watcher import discover_skill_roots

            connected = list(settings.skill_folders)
            lines = ["Connected skill folders:"] + ([f"  {path}" for path in connected] or ["  (none)"])
            found = [(path, count) for path, count in discover_skill_roots() if path not in connected]
            if found:
                lines.append("Other skill folders on this PC (add them under Settings → Plugins → triage → Skill folders):")
                lines += [f"  {path}  ({count} skills)" for path, count in found]
            return "\n".join(lines)
        if text.startswith("use "):
            name = text[4:].strip()
            return self._force_named(session_id, name, why="slash use")
        if text.startswith("find "):
            return self._find_catalog(text[5:].strip())
        named = self.store.get(text)
        if named is not None:
            return self._force_named(session_id, named.name, why="slash use")
        query = text[5:].strip() if text.startswith("peek ") else text
        peek_only = text.startswith("peek ")
        ranking = self.judge.rank(query, limit=settings.shortlist_size)
        if not ranking.candidates:
            return "No skill scored above the noise floor for that message.\n" + render_shortlist(ranking)
        if peek_only or not settings.interactive:
            self.memory.remember(session_id, ranking)
            return render_shortlist(ranking)
        ask = self._ask()
        if ask is None:
            self.memory.remember(session_id, ranking)
            return render_shortlist(ranking) + "\n\nReply `/triage use <name>` to load one."
        choices = [
            {
                "value": item.name,
                "label": item.label(),
                "description": item.description[:160],
            }
            for item in ranking.candidates
        ]
        choices.append(
            {
                "value": "__skip__",
                "label": f"{_pct(ranking.none_probability)} skip — load nothing",
                "description": "Continue with no skill body",
            }
        )
        try:
            picked = ask("Which skill should Hermes load?", choices)
        except Exception:
            logger.exception("ask_choice failed")
            picked = None
        picked = _coerce_choice(picked, ranking)
        if picked in (None, ""):
            self.memory.remember(session_id, ranking)
            return render_shortlist(ranking) + "\n\nNo choice recorded. Reply with a name or number."
        if picked == "__skip__":
            self.store.log_selection(
                turn_id="",
                session_id=session_id,
                query=query,
                candidates=ranking.as_log(),
                chosen=None,
                accepted=False,
            )
            return "No skill loaded."
        return self._force_named(session_id, str(picked), why="slash choice", ranking=ranking)

    def _find_catalog(self, query: str) -> str:
        needle = (query or "").strip().casefold()
        skills = self.store.list_skills()
        if not needle:
            return f"{len(skills)} skills in the catalog. Try `/triage find <name>` or `/triage use <name>`."
        hits = [
            skill for skill in skills
            if needle in skill.name.casefold() or needle in (skill.description or "").casefold()
        ]
        if not hits:
            return f"No catalog name matches {query!r}. `/triage use <name>` loads any stored skill."
        shown = hits[:40]
        lines = [f"{skill.name} — {skill.description}" for skill in shown]
        if len(hits) > len(shown):
            lines.append(f"… {len(hits) - len(shown)} more. `/triage use <name>` loads any of them.")
        else:
            lines.append("Load one with `/triage use <name>`.")
        return "\n".join(lines)

    def _force_named(self, session_id: str, name: str, *, why: str, ranking: Ranking | None = None) -> str:
        skill = self.store.get(name)
        if skill is None:
            return (
                f"Unknown skill {name!r}. "
                "The catalog is searched by name: `/triage find <text>` or `/triage use <name>`."
            )
        probability = 0.0
        if ranking is not None:
            for item in ranking.candidates:
                if item.name.casefold() == skill.name.casefold():
                    probability = item.probability
                    why = item.why
                    break
        self.memory.force(session_id, skill.name, probability, why)
        self.store.enqueue_force(session_id, skill.name, probability, why)
        self.store.log_selection(
            turn_id="",
            session_id=session_id,
            query="",
            candidates=ranking.as_log() if ranking else [{"name": skill.name, "p": probability, "why": why}],
            chosen=skill.name,
            accepted=True,
        )
        shown = _pct(probability) if probability else "chosen"
        return (
            f"Approved {skill.name} ({shown}). "
            "Your next message loads only that skill body into the model context. "
            "skill_view of any other name stays blocked for that turn."
        )

    def _maybe_ask(self, settings: Settings, platform: str, ranking: Ranking) -> str | None:
        if not settings.interactive:
            return None
        if platform.casefold() in _NONINTERACTIVE_PLATFORMS:
            return None
        ask = self._ask()
        if ask is None:
            return None
        choices = [{"value": item.name, "label": item.label(), "description": item.description[:160]} for item in ranking.candidates]
        choices.append({"value": "__skip__", "label": f"{_pct(ranking.none_probability)} skip — load nothing"})
        try:
            picked = ask("Which skill should Hermes load?", choices)
        except Exception:
            logger.exception("ask_choice from pre_llm_call failed")
            return None
        return _coerce_choice(picked, ranking)

    def _ask(self):
        if self._ask_override is not None:
            return self._ask_override
        if self.ctx is None:
            return None
        return getattr(self.ctx, "ask_choice", None)

    def _ensure_embedder(self, settings: Settings) -> None:
        if settings.embedder == self._embedder_path:
            return
        self._embedder_path = settings.embedder
        self.judge.embedder = load_embedder(settings.embedder)

    def _log(self, turn_id, session_id, query, candidates, chosen, accepted, probability, why) -> None:
        if not candidates and chosen:
            candidates = [{"name": chosen, "p": probability, "why": why}]
        try:
            self.store.log_selection(
                turn_id=turn_id,
                session_id=session_id,
                query=query[:500],
                candidates=candidates,
                chosen=chosen,
                accepted=accepted,
            )
        except Exception:
            logger.exception("selection log failed")


def _join_context(bridge: str | None, rest: str | None) -> str:
    parts = [part for part in (bridge, rest) if part]
    return "\n\n".join(parts)


def _thought_text(kwargs: dict) -> str:
    """Optional thought/trace text. Hermes v0.22 does not send this on pre_llm_call."""
    parts: list[str] = []
    for key in ("thoughts", "reasoning", "trace"):
        value = kwargs.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return "\n".join(parts)


def scope_key(turn_id: str, session_id: str) -> str:
    if turn_id:
        return f"turn:{turn_id}"
    if session_id:
        return f"session:{session_id}"
    return "turn:default"


def message_text(user_message) -> str:
    if user_message is None:
        return ""
    if isinstance(user_message, str):
        return user_message
    if isinstance(user_message, list):
        parts: list[str] = []
        for item in user_message:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
        return "\n".join(parts)
    return str(user_message)


def parse_pick(message: str, pending: Ranking | None) -> str | None:
    """Return a skill name, ``__skip__``, or ``None`` when the message is a new query.

    Only the whole message counts, so a normal sentence is judged again instead
    of accidentally loading a skill whose name appears inside it.
    """
    if pending is None or not pending.candidates:
        return None
    text = message.strip().strip("`'\".").strip()
    lowered = text.casefold()
    if lowered in _SKIP:
        return "__skip__"
    names = [item.name for item in pending.candidates]
    if lowered.isdigit():
        index = int(lowered) - 1
        if 0 <= index < len(names):
            return names[index]
        return None
    for prefix in ("use ", "pick ", "load ", "skill "):
        if lowered.startswith(prefix):
            lowered = lowered[len(prefix) :].strip()
            text = text.split(None, 1)[1].strip() if " " in text else text
            break
    for name in names:
        if lowered == name.casefold():
            return name
    return None


# Always-load shares the hook output with whatever the judge adds on the same
# turn, so its inline share stays well under Hermes' 10k spill limit. Bodies
# that do not fit are approved and pointed at skill_view (served in full).
ALWAYS_LOAD_INLINE_BUDGET = HOOK_CONTEXT_SAFE_CHARS // 2


def render_always_load(
    skills: list[Skill],
    missing: list[str],
    project_file: str = "",
    *,
    header: str | None = None,
    budget: int = ALWAYS_LOAD_INLINE_BUDGET,
) -> str:
    if not skills and not missing:
        return ""
    source = "global setting" + (f" + {project_file}" if project_file else "")
    lines = [
        header
        or (
            f"[triage] Always-load skills for this session ({source}). "
            "They are approved for the whole session."
        )
    ]
    inline: list[str] = []
    deferred: list[Skill] = []
    used = len(lines[0])
    for skill in skills:
        block = f"# {skill.name}\n\n{skill.description}\n\n{skill.body}".strip()
        if used + len(block) + 2 <= budget:
            inline.append(block)
            used += len(block) + 2
        else:
            deferred.append(skill)
    if deferred:
        lines.append(
            "Too large to inline here. Before answering, call skill_view for each and follow it: "
            + ", ".join(f"skill_view(name={skill.name!r})" for skill in deferred)
            + "."
        )
    if missing:
        lines.append("Not in the catalog (skipped): " + ", ".join(missing) + ".")
    return "\n".join(lines) + ("\n\n" + "\n\n".join(inline) if inline else "")


_WRITE_ACTIONS = frozenset({"create", "patch", "edit", "write_file"})


def _writes(args: dict) -> bool:
    """True when a skill_manage call (or any operation of a batch) creates or edits a skill."""
    ops = args.get("operations") if isinstance(args.get("operations"), list) else [args]
    return any(isinstance(op, dict) and str(op.get("action") or "").strip().lower() in _WRITE_ACTIONS for op in ops)


def _hermes_config() -> dict:
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly()
    except Exception:
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _guard_agent_created() -> bool:
    """Hermes' opt-in scan of skills the agent writes (``skills.guard_agent_created``)."""
    value = (_hermes_config().get("skills") or {}).get("guard_agent_created")
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _hermes_skill_roots() -> list[Path]:
    """Hermes' own skill folders: ``<hermes home>/skills`` and ``skills.external_dirs``."""
    from .paths import default_skills_dir

    roots = [default_skills_dir()]
    extra = (_hermes_config().get("skills") or {}).get("external_dirs") or []
    if isinstance(extra, str):
        extra = [part for part in extra.replace(";", ",").split(",")]
    for item in extra if isinstance(extra, list) else []:
        text = str(item or "").strip()
        if text:
            roots.append(Path(text).expanduser())
    return roots


def _under_any(path: str, roots: list[Path]) -> bool:
    if not path:
        return False
    try:
        target = Path(path).resolve()
    except (OSError, ValueError):
        return False
    for root in roots:
        try:
            target.relative_to(root.resolve())
            return True
        except (OSError, ValueError):
            continue
    return False


def _manage_names(args: dict) -> list[str]:
    names: list[str] = []
    if isinstance(args.get("operations"), list):
        for op in args["operations"]:
            if isinstance(op, dict) and (op.get("name") or args.get("name")):
                names.append(str(op.get("name") or args.get("name")))
    elif args.get("name"):
        names.append(str(args["name"]))
    return names


def _auto_evidence(store, text: str, name: str) -> bool:
    from .context import auto_evidence

    skill = store.get(name)
    return skill is not None and auto_evidence(store, text, skill)


# Platforms whose UI shows the chips; there the server never decides.
_CHIP_PLATFORMS = frozenset({"desktop", "local"})  # Hermes Desktop chats are "local"


def already_loaded_view(skill: Skill) -> str:
    """skill_view answer for a skill whose full text is already in the chat.

    Re-sending the body would put it in the context twice (a failure other
    skill routers hit when Hermes did not know a hook had injected it)."""
    import json

    return json.dumps(
        {
            "success": True,
            "name": skill.name,
            "description": skill.description,
            "content": (
                f"Already loaded: the full text of {skill.name} was added to this conversation "
                "with the user's message. Follow it from there. For a linked file, call "
                "skill_view(name, file_path)."
            ),
            "served_by": "triage",
        },
        ensure_ascii=False,
    )


def render_loaded(skill: Skill, probability: float | None, why: str) -> str:
    pct = f" ({_pct(probability)})" if probability else ""
    reason = f" — {why}" if why else ""
    full = (
        f"[triage] Loaded skill {skill.name}{pct}{reason}. "
        "Follow this skill only. No other skill body was injected.\n\n"
        f"# {skill.name}\n\n"
        f"{skill.description}\n\n"
        f"{skill.body}"
    )
    return activation_text(skill, probability=probability, why=why, full=full)


def _args(args) -> dict:
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        import json

        try:
            parsed = json.loads(args)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def render_shortlist(ranking: Ranking, unlocked: list[str] | None = None) -> str:
    unlocked = [name for name in (unlocked or []) if name]
    if unlocked:
        lines = [
            "[triage] The system-prompt skills index is withheld.",
            "Unlocked in this chat (bodies may have been compacted). "
            "skill_view is allowed for: " + ", ".join(unlocked) + ".",
            "The list below is a fresh shortlist. Nothing new is loaded until you pick one or skip.",
            f"No-skill probability: {_pct(ranking.none_probability)}.",
        ]
    else:
        lines = [
            "[triage] The system-prompt skills index is withheld. "
            "Nothing below is loaded yet.",
            "These skills may fit this message. Answer the message normally. If one clearly fits, "
            "add one short line naming it (the user can reply with its number or name to load it); "
            "otherwise ignore this list.",
            "Do not call skill_view until one of them is approved.",
            f"No-skill probability: {_pct(ranking.none_probability)}.",
        ]
    if not ranking.candidates:
        lines.append("(no skill scored above the noise floor)")
        return "\n".join(lines)
    for index, candidate in enumerate(ranking.candidates, start=1):
        lines.append(f"{index}. {candidate.label()}")
    return "\n".join(lines)


def _coerce_choice(picked, ranking: Ranking) -> str | None:
    """Normalize ask_choice's return into a skill name, ``__skip__``, or None.

    Hosts return the choice ``value``. A host that echoes the visible label
    still resolves, because the label begins with the probability and the name.
    """
    if picked in (None, ""):
        return None
    text = str(picked).strip()
    if text == "__skip__" or text.casefold() in _SKIP:
        return "__skip__"
    for item in ranking.candidates:
        if text.casefold() == item.name.casefold() or text == item.label():
            return item.name
    return text


def _pending_meta(pending: Ranking | None, name: str) -> tuple[float, str]:
    if pending is None:
        return 0.0, "picked from shortlist"
    for item in pending.candidates:
        if item.name.casefold() == name.casefold():
            return item.probability, item.why
    return 0.0, "picked from shortlist"


def _empty_catalog_note() -> str:
    return (
        "[triage] Skill store is empty, so no skill body will be injected. "
        "Run `python run.py migrate --from ~/.hermes/skills` (or examples/demo-skills)."
    )


def _command_help() -> str:
    return (
        "Usage:\n"
        "/triage <message>   rank and ask which skill to load\n"
        "/triage peek <message>   show probabilities without loading\n"
        "/triage use <name>  approve any catalog skill for the next message\n"
        "/triage find <text> search the full catalog by name\n"
        "/triage list        list the plugin store\n"
        "Hermes v0.22 cannot open ask_choice from pre_llm_call (it returns None). "
        "Ordinary turns inject the probability shortlist into the user message instead."
    )


def _pct(probability: float | None) -> str:
    from .judge import format_percent

    return format_percent(probability or 0.0)


def _clamp_threshold(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.90
    if number < 0:
        return 0.0
    if number > 1:
        return 1.0
    return number


def _clamp_size(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 5
    return max(1, min(number, 8))


def _clamp_attached(value) -> int:
    """0 (the default) means no limit; otherwise at least 1."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


_RUNTIME: Runtime | None = None
_RUNTIME_LOCK = threading.Lock()


def install_runtime(runtime: Runtime) -> None:
    global _RUNTIME
    with _RUNTIME_LOCK:
        _RUNTIME = runtime


def current_runtime() -> Runtime | None:
    with _RUNTIME_LOCK:
        return _RUNTIME
