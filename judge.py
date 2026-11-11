"""Full-text judge over skill content, with an optional embedding hook.

Lexical ranking uses the whole SKILL.md plus indexed reference text. A
near-exact sentence from the body outranks a name-only hit. When an
embedder is configured, its ranking is fused with the lexical ranking by
reciprocal rank fusion (RRF). The two scores are never added together.

The query is the newest user message, with earlier user turns, clipped
assistant text, and optional thoughts as weaker context.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from typing import Protocol

from .store import Skill, SkillStore

# Competing mass for "load nothing". A lone description overlap (about 1.5–3)
# stays under this, so the default auto-accept threshold does not fire.
# A hyphenated name mention plus description overlap clears it.
# A verbatim sentence from the body scores far above this (~95% and up).
NONE_PRIOR = 6.0
RRF_K = 60
# Relative BM25 on the latest user text. A clear leader keeps more of the
# gap than a flat cap, but the bonus stays small so a short query cannot
# auto-accept on retrieval strength alone.
_BM25_CAP = 1.25
_HISTORY_CAP = 0.75
_ASSISTANT_CAP = 0.35
_THOUGHT_CAP = 0.25
# One solid latest-user hit (a name token, or a pair of description hits).
# Above this, history cannot vote for a skill the new message does not support.
_FOCUS_CLEAR = 3.0
_FOCUS_KEEP = 0.45
_SCORE_CAP_POOL = 12
# Context channel: the strongest context-only skill gets this much — under
# NONE_PRIOR, so background alone never looks like a sure pick. Auto-accept
# is decided on a context-free ranking (service.py), never on this channel.
_CONTEXT_CAP = 4.5
_CONTEXT_NAME_HIT = 0.6
_CONTEXT_TERMS = 12
_STOP = frozenset(
    """
    a an the to for of and or in on my me i you your please help with from
    this that is are was be do does it if at as by we our can could would
    should just into onto about
    want wants tell look take give show let lets know think try need needs
    something anything everything thing things what does did done got get
    some any all have has had will maybe really also like okay ok yes not
    then than there here now how why when where which who its
    """.split()
)
# Words too common in chat and tool output to say what a conversation is about.
_GENERIC = frozenset(
    """
    not no yes have has had will what when where which who why how all any some
    more most other than then them they their there these those its also only
    very much many like want need make made get got use used using run file files
    output error true false null none content name path line lines text value
    new one two see look tell know think way thing things here now out up down
    just so but too off over again still each every both same such own while
    json md http https www com org exit code users local appdata
    """.split()
    # The account name is in every path a tool prints; it is not a topic.
    + [os.path.basename(os.path.expanduser("~")).casefold()]
)


class Embedder(Protocol):
    def score(self, query: str, skill: Skill) -> float | None:
        """Return a similarity in ``[0, 1]``, or ``None`` to abstain.

        The value is ranked, then fused with the lexical ranking by RRF.
        It is not added to the lexical point score. A later MiniLM embedder
        can implement this without changing the judge.
        """


@dataclass(frozen=True)
class AssembledQuery:
    """Conversation window used for one ranking.

    ``latest_user`` is weighted fully. Earlier user text counts less.
    Assistant text and thoughts are clipped and capped so a long dump
    cannot outvote the newest request. Hermes v0.22 does not pass a
    thought channel into ``pre_llm_call``; ``thoughts`` stays empty unless
    a host adds ``thoughts``, ``reasoning``, or ``trace``.
    """

    latest_user: str
    earlier_user: str = ""
    assistant: str = ""
    thoughts: str = ""
    # Background the chat is about: first prompt + attachments, workspace
    # AGENTS.md/README, recent replies and tool reads. Scored through its own
    # keyword BM25 channel (capped), so the chips follow the line of thought.
    context: str = ""

    @property
    def focus_text(self) -> str:
        return self.latest_user or self.earlier_user


@dataclass(frozen=True)
class Candidate:
    name: str
    description: str
    probability: float
    score: float
    why: str

    def label(self) -> str:
        why = self.why if len(self.why) <= 88 else self.why[:87].rstrip() + "…"
        return f"{format_percent(self.probability)} {self.name} — {why}"


@dataclass(frozen=True)
class Ranking:
    query: str
    candidates: list[Candidate]
    none_probability: float

    def as_log(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = [
            {"name": item.name, "p": round(item.probability, 4), "why": item.why}
            for item in self.candidates
        ]
        rows.append({"name": None, "p": round(self.none_probability, 4), "why": "no skill"})
        return rows


class SkillJudge:
    """Rank store skills against the conversation window.

    ``embedder`` is optional. With no embedder the judge is lexical only:
    FTS5 BM25 over the full body and references, plus a verbatim-sentence
    bonus. With an embedder, lexical and embedding ranks are fused by RRF.
    """

    def __init__(self, store: SkillStore, embedder: Embedder | None = None):
        self.store = store
        self.embedder = embedder

    def rank(
        self,
        query: str | AssembledQuery,
        *,
        limit: int = 5,
        history: list | None = None,
        thoughts: str = "",
        context: str = "",
        independent: bool = False,
    ) -> Ranking:
        """Rank skills. ``independent=False`` (the turn decision) shares one
        probability mass between the candidates and "no skill", so it names a
        single best skill. ``independent=True`` (the chips) scores each skill
        against "no skill" alone, so several strong matches can all be high
        and each can clear the auto-attach threshold on its own."""
        limit = max(1, min(int(limit), 32))
        if isinstance(query, str):
            query = fix_typos(query, self.store)
        assembled = (
            query
            if isinstance(query, AssembledQuery)
            else assemble_conversation_query(query, history, thoughts=thoughts, context=context)
        )
        context_terms = context_keywords(assembled.context)
        context_bm25 = (
            self.store.fts_bm25(" ".join(context_terms), limit=40) if context_terms else {}
        )
        skills = self._candidates(assembled, extra=context_bm25)
        focus_terms = " ".join(tokenize(assembled.focus_text))
        bm25 = self.store.fts_bm25(focus_terms, limit=max(limit * 4, 20)) if focus_terms else {}
        focus_rows: list[tuple[Skill, float, str, float, str]] = []
        for skill in skills:
            focus, focus_why = self._focus(assembled, skill, _relative_bm25(bm25, skill.name))
            history_score, history_why = self._history(assembled, skill)
            focus_rows.append((skill, focus, focus_why, history_score, history_why))
        best_focus = max((row[1] for row in focus_rows), default=0.0)
        topic_locked = best_focus >= _FOCUS_CLEAR
        lexical_rows: list[tuple[Skill, float, str]] = []
        embed_scores: dict[str, float] = {}
        for skill, focus, focus_why, history_score, history_why in focus_rows:
            if topic_locked and focus < best_focus * _FOCUS_KEEP:
                history_score = 0.0
                history_why = ""
            else:
                history_score = min(_HISTORY_CAP, history_score)
            context_score, context_why = _context_score(context_bm25, context_terms, skill)
            lexical = focus + history_score + context_score
            why = _join_why(focus_why, history_why, context_why)
            embed = self._embed(assembled.focus_text, skill)
            if embed is not None:
                embed_scores[skill.name] = max(0.0, min(1.0, embed))
            if lexical <= 0 and embed is None:
                continue
            lexical_rows.append((skill, lexical, why or "weak overlap"))
        if not lexical_rows and not embed_scores:
            return Ranking(query=assembled.focus_text, candidates=[], none_probability=1.0)

        lexical_rows.sort(key=lambda item: (-item[1], item[0].name))
        use_embed = bool(embed_scores)
        if use_embed:
            lexical_names = [skill.name for skill, score, _why in lexical_rows if score > 0]
            embed_names = sorted(embed_scores, key=lambda name: (-embed_scores[name], name))
            fused = reciprocal_rank_fusion([lexical_names, embed_names])
            by_name = {skill.name: (skill, score, why) for skill, score, why in lexical_rows}
            ordered_names = sorted(fused, key=lambda name: (-fused[name], name))
        else:
            fused = {}
            ordered_names = [skill.name for skill, _score, _why in lexical_rows]
            by_name = {skill.name: (skill, score, why) for skill, score, why in lexical_rows}

        lexical_positive = [(skill, score, why) for skill, score, why in lexical_rows if score > 0]
        lexical_probs: dict[str, float] = {}
        none_p = 1.0
        if lexical_positive:
            pool = lexical_positive[: max(limit * 3, limit)]
            if independent:
                for skill, score, _why in pool:
                    lexical_probs[skill.name] = softmax([score, NONE_PRIOR])[0]
                none_p = 1.0 - max(lexical_probs.values())
            else:
                weights = [score for _skill, score, _why in pool] + [NONE_PRIOR]
                probs = softmax(weights)
                none_p = probs[-1]
                for index, (skill, _score, _why) in enumerate(pool):
                    lexical_probs[skill.name] = probs[index]
        else:
            none_p = 0.55

        candidates: list[Candidate] = []
        for name in ordered_names:
            skill, score, why = by_name[name]
            probability = lexical_probs.get(name, 0.0)
            embed_value = embed_scores.get(name, 0.0)
            if embed_value > 0:
                why = _join_why(why, f"embed {embed_value:.2f}", "rrf")
                embed_probability = min(0.45, 0.2 + 0.25 * embed_value)
                if probability < embed_probability:
                    probability = embed_probability
            elif use_embed and name in fused:
                why = _join_why(why, "rrf")
            if probability < 0.02:
                continue
            candidates.append(
                Candidate(
                    name=skill.name,
                    description=skill.description,
                    probability=probability,
                    score=fused.get(name, score),
                    why=why or "weak overlap",
                )
            )
            if len(candidates) >= limit:
                break
        if not candidates:
            return Ranking(query=assembled.focus_text, candidates=[], none_probability=1.0)
        shown = sum(item.probability for item in candidates)
        if not independent and shown + none_p > 1.0:
            none_p = max(0.0, 1.0 - shown)
        return Ranking(query=assembled.focus_text, candidates=candidates, none_probability=none_p)

    def _candidates(self, assembled: AssembledQuery, extra: dict[str, float] | None = None) -> list[Skill]:
        skills = self.store.list_skills()
        if len(skills) <= 200:
            return skills
        names = (self.store.fts_names(assembled.focus_text, limit=_SCORE_CAP_POOL * 4) or []) if assembled.focus_text else []
        names = list(names) + list((extra or {}).keys())
        if not names:
            return skills if assembled.focus_text else []
        wanted = {name.casefold() for name in names}
        lowered = assembled.focus_text.casefold()
        filtered = [
            skill
            for skill in skills
            if skill.name.casefold() in wanted or skill.name.casefold() in lowered
        ]
        return filtered or skills

    def _focus(self, assembled: AssembledQuery, skill: Skill, bm25_bonus: float) -> tuple[float, str]:
        """Score the newest user message only. This is what can auto-accept."""
        reasons: list[str] = []
        score = _surface_score(assembled.latest_user, skill, reasons, weight=1.0)
        span = longest_verbatim_span(assembled.latest_user, _skill_text(skill)[3], normalized=True)
        if span >= 5:
            score += 22.0 + 2.0 * span
            reasons.append(f"body sentence ({span} words)")
        if bm25_bonus > 0:
            score += bm25_bonus
            reasons.append("bm25")
        why = "; ".join(dict.fromkeys(reasons))
        return score, why

    def _history(self, assembled: AssembledQuery, skill: Skill) -> tuple[float, str]:
        """Soft context. Dropped when the newest message clearly names another topic."""
        reasons: list[str] = []
        score = 0.0
        if assembled.earlier_user:
            score += _surface_score(assembled.earlier_user, skill, [], weight=0.15)
        assistant_points = min(_ASSISTANT_CAP, _body_token_points(assembled.assistant, skill) * 0.1)
        if assistant_points > 0:
            score += assistant_points
            reasons.append("assistant context")
        thought_points = min(_THOUGHT_CAP, _body_token_points(assembled.thoughts, skill) * 0.1)
        if thought_points > 0:
            score += thought_points
            reasons.append("thought context")
        if score <= 0:
            return 0.0, ""
        return min(_HISTORY_CAP, score), "; ".join(dict.fromkeys(reasons))

    def _embed(self, query: str, skill: Skill) -> float | None:
        embedder = self.embedder
        if embedder is None:
            return None
        try:
            value = embedder.score(query, skill)
        except Exception:
            return None
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


def assemble_conversation_query(
    user_message: str, history: list | None = None, *, thoughts: str = "", context: str = ""
) -> AssembledQuery:
    """Build the ranking window from Hermes' ``pre_llm_call`` payload.

    ``conversation_history`` is the OpenAI-style message list when the host
    sends one. Thoughts are not part of that hook today; pass them only when
    a host includes ``thoughts``, ``reasoning``, or ``trace``.
    """
    latest = message_text(user_message).strip()
    users: list[str] = []
    assistants: list[str] = []
    thought_bits: list[str] = []
    for message in history or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        content = _clip(message_text(message.get("content")), 160 if role == "assistant" else 800)
        if not content:
            continue
        if role == "user":
            users.append(content)
        elif role == "assistant":
            assistants.append(content)
        elif role in {"thought", "reasoning", "tool", "system"}:
            if role != "system":
                thought_bits.append(_clip(content, 300))
    if users and users[-1].strip() == latest:
        earlier = users[:-1]
    else:
        earlier = users
    earlier_text = "\n".join(earlier[-2:])
    # One short assistant tail. A long previous answer must not keep its skill on top.
    assistant_text = assistants[-1] if assistants else ""
    thought_text = thoughts.strip() or "\n".join(thought_bits[-1:])
    return AssembledQuery(
        latest_user=latest,
        earlier_user=_clip(earlier_text, 800),
        assistant=_clip(assistant_text, 700),
        thoughts=_clip(thought_text, 400),
        context=_clip(context, 20000),
    )


def message_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
        return "\n".join(parts)
    return str(value)


def reciprocal_rank_fusion(rankings: list[list[str]], k: int = RRF_K) -> dict[str, float]:
    """Fuse ranked name lists. Rank 1 scores ``1/(k+1)``. Higher is better."""
    fused: dict[str, float] = {}
    for ranking in rankings:
        for index, name in enumerate(ranking):
            fused[name] = fused.get(name, 0.0) + 1.0 / (k + index + 1)
    return fused


def longest_verbatim_span(query: str, document: str, *, normalized: bool = False) -> int:
    """Content-word count of the longest query span that appears in ``document``.

    Spans shorter than five content words do not count. The document is the
    full skill body plus references, not a 4k prefix.
    """
    doc = document if normalized else _normalize(document)
    if not doc or not query:
        return 0
    best = 0
    for sentence in re.split(r"(?<=[.!?])\s+", query):
        words = _content_words(sentence)
        if len(words) < 5:
            continue
        if _normalize(sentence) in doc:
            best = max(best, len(words))
    tokens = [token for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]*", query)]
    for size in range(min(14, len(tokens)), 4, -1):
        if best >= size:
            break
        for start in range(0, len(tokens) - size + 1):
            window = " ".join(tokens[start : start + size])
            words = _content_words(window)
            if len(words) < 5:
                continue
            if _normalize(window) in doc:
                return max(best, len(words))
    return best


def context_keywords(text: str, limit: int = _CONTEXT_TERMS) -> list[str]:
    """The terms that say what a conversation is about: frequent, specific,
    not chat filler. Order is by weight, so the FTS query keeps the best ones."""
    if not text:
        return []
    counts: dict[str, int] = {}
    for token in tokenize(text):
        if len(token) < 3 or token in _GENERIC or token.isdigit():
            continue
        counts[token] = counts.get(token, 0) + 1
    ranked = sorted(counts, key=lambda token: (-(counts[token] * min(len(token), 10)), token))
    return ranked[:limit]


def _context_score(strengths: dict[str, float], terms: list[str], skill: Skill) -> tuple[float, str]:
    score = 0.0
    reasons: list[str] = []
    mine = strengths.get(skill.name, 0.0)
    best = max(strengths.values(), default=0.0)
    if mine > 0 and best > 0:
        score += _CONTEXT_CAP * 0.75 * (mine / best)
    if terms:
        name_tokens = set(tokenize(skill.name.replace("-", " ").replace("_", " ")))
        hits = [token for token in terms if token in name_tokens]
        if hits:
            score += min(_CONTEXT_CAP * 0.5, _CONTEXT_NAME_HIT * len(hits))
            reasons.append("context " + ", ".join(hits[:3]))
    score = min(_CONTEXT_CAP, score)
    if score > 0 and not reasons:
        reasons.append("context")
    return score, "; ".join(reasons)


def _relative_bm25(strengths: dict[str, float], name: str) -> float:
    """Keep a real BM25 lead without letting raw magnitudes auto-accept.

    The leader of a separated match gets the full cap. A near-tie gets a
    fraction, so two skills that both mention "commit" do not both look certain.
    """
    mine = strengths.get(name, 0.0)
    if mine <= 0:
        return 0.0
    ordered = sorted(strengths.values(), reverse=True)
    best = ordered[0]
    second = ordered[1] if len(ordered) > 1 else 0.0
    if best <= 0:
        return 0.0
    share = mine / best
    margin = max(0.0, (mine - second) / best)
    if mine >= best and margin >= 0.2:
        return _BM25_CAP
    return min(_BM25_CAP * 0.4, _BM25_CAP * share * 0.5)


def _surface_score(text: str, skill: Skill, reasons: list[str], *, weight: float) -> float:
    local: list[str] = []
    points = _name_and_description(text, skill, local) + _body_token_points(text, skill, local)
    if weight >= 1.0:
        reasons.extend(local)
    return points * weight


def _name_and_description(text: str, skill: Skill, reasons: list[str]) -> float:
    if not text:
        return 0.0
    score = 0.0
    lowered = text.casefold()
    tokens = set(tokenize(text))
    phrase = skill.name.casefold().replace("_", "-")
    # Whole-word only. A bare substring test lets a skill named "c" or "dep"
    # claim every draft containing "container" or "deploy" — invisible with a
    # few dozen skills, dominant with thousands.
    if phrase and re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", lowered):
        score += 8
        reasons.append(f"name {skill.name}")
    name_words, desc_words, _body, _doc = _skill_text(skill)
    name_hits = sorted(name_words & tokens)
    if name_hits:
        score += 3 * len(name_hits)
        reasons.append("name " + ", ".join(name_hits))
    desc_hits = sorted(desc_words & tokens)
    if desc_hits:
        score += 1.5 * len(desc_hits)
        reasons.append("description " + ", ".join(desc_hits[:4]))
    return score


# Per-skill word sets and normalised text, built once per skill version: a
# skill's body and references only change when the skill does, and splitting
# them on every keystroke was most of a ranking pass.
_SKILL_TEXT: dict[tuple, tuple] = {}


def _skill_text(skill: Skill) -> tuple[frozenset, frozenset, frozenset, str]:
    key = (skill.name, getattr(skill, "content_hash", ""), len(skill.body), len(skill.refs or ""))
    hit = _SKILL_TEXT.get(key)
    if hit is None:
        document = f"{skill.body}\n{skill.refs}"
        hit = (
            frozenset(tokenize(skill.name.replace("-", " ").replace("_", " "))),
            frozenset(tokenize(skill.description)),
            frozenset(tokenize(document)),
            _normalize(document),
        )
        if len(_SKILL_TEXT) > 6000:
            _SKILL_TEXT.clear()
        _SKILL_TEXT[key] = hit
    return hit


def _body_token_points(text: str, skill: Skill, reasons: list[str] | None = None) -> float:
    if not text:
        return 0.0
    tokens = set(tokenize(text))
    name_hits, desc_hits, body_words, _doc = _skill_text(skill)
    body_hits = sorted(body_words & tokens - name_hits - desc_hits)
    if not body_hits:
        return 0.0
    if reasons is not None:
        reasons.append("body " + ", ".join(body_hits[:3]))
    return min(3.0, 0.35 * len(body_hits))


def _content_words(text: str) -> list[str]:
    return tokenize(text)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").casefold()).strip()


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit]


def _join_why(*parts: str) -> str:
    seen: list[str] = []
    for part in parts:
        for piece in part.split("; "):
            piece = piece.strip()
            if piece and piece not in seen:
                seen.append(piece)
    return "; ".join(seen)


_TYPO_CUTOFF = 0.8
_TYPO_CACHE: dict[tuple, str] = {}


def fix_typos(text: str, store) -> str:
    """Replace words that occur nowhere in the catalog with the closest skill word.

    "andriod crash" becomes "android crash". Only words of 4+ letters that the
    index has never seen are touched, and only when a name/description/tag word
    is at least 80% similar, so real words and rare terms pass through. Cheap:
    one index lookup per word, cached."""
    if not text or store is None or not hasattr(store, "has_term"):
        return text
    import difflib

    changed = False
    out: list[str] = []
    for word in re.split(r"(\W+)", text):
        lower = word.lower()
        if len(lower) < 4 or not lower.isalpha() or lower in _STOP:
            out.append(word)
            continue
        key = (str(getattr(store, "db_path", "")), lower)
        fixed = _TYPO_CACHE.get(key)
        if fixed is None:
            fixed = lower
            if not store.has_term(lower):
                close = difflib.get_close_matches(lower, store.vocabulary(), n=1, cutoff=_TYPO_CUTOFF)
                if close:
                    fixed = close[0]
            if len(_TYPO_CACHE) > 4096:
                _TYPO_CACHE.clear()
            _TYPO_CACHE[key] = fixed
        if fixed != lower:
            changed = True
            out.append(fixed)
        else:
            out.append(word)
    return "".join(out) if changed else text


def tokenize(text: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(token) > 1 and token not in _STOP
    ]


def softmax(values: list[float]) -> list[float]:
    if not values:
        return []
    peak = max(values)
    exps = [math.exp(value - peak) for value in values]
    total = sum(exps) or 1.0
    return [item / total for item in exps]


def format_percent(probability: float) -> str:
    pct = max(0.0, probability) * 100
    if pct >= 10:
        return f"{pct:.0f}%"
    if pct >= 1:
        return f"{pct:.1f}%"
    if pct > 0:
        return "<1%"
    return "0%"


def load_embedder(dotted: str) -> Embedder | None:
    """Import ``pkg.mod.Object`` with a ``score`` method.

    A class is instantiated with no arguments. A zero-arg factory is called
    when the attribute itself has no ``score``. Empty string returns ``None``.
    Import errors return ``None`` so a bad setting cannot break the turn.
    """
    dotted = (dotted or "").strip()
    if not dotted:
        return None
    module_name, sep, attr = dotted.rpartition(".")
    if not sep or not module_name or not attr:
        return None
    try:
        import importlib

        module = importlib.import_module(module_name)
        obj = getattr(module, attr)
        if isinstance(obj, type):
            obj = obj()
        elif callable(obj) and not hasattr(obj, "score"):
            obj = obj()
    except Exception:
        return None
    if not hasattr(obj, "score"):
        return None
    return obj
