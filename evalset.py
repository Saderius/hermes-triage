"""Measure the chip ranking and auto-attach on labelled drafts."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .context import chip_ranking, strip_injected
from .judge import SkillJudge

THRESHOLDS = (0.50, 0.60, 0.70, 0.80, 0.85, 0.90, 0.95, 0.99)


@dataclass(frozen=True)
class Case:
    draft: str
    expect: tuple[str, ...] | None  # None = not labelled; () = no skill wanted
    kind: str = ""


def load_cases(path: Path) -> list[Case]:
    cases: list[Case] = []
    text = Path(path).read_text(encoding="utf-8")
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            data = json.loads(line)
        except Exception as err:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {err}") from err
        if not isinstance(data, dict):
            raise ValueError(f"{path}:{line_number}: expected JSON object")
        draft = data.get("draft")
        if not isinstance(draft, str) or not draft.strip():
            raise ValueError(f"{path}:{line_number}: missing or empty 'draft'")

        expect_raw = data.get("expect")
        if "expect" not in data or expect_raw is None:
            expect = None
        elif isinstance(expect_raw, list):
            cleaned = []
            for item in expect_raw:
                if isinstance(item, str):
                    s = item.strip()
                    if s:
                        cleaned.append(s)
            expect = tuple(cleaned)
        else:
            raise ValueError(f"{path}:{line_number}: 'expect' must be a list or null")

        kind = str(data.get("kind") or "")
        cases.append(Case(draft=draft, expect=expect, kind=kind))
    return cases


def snapshot(db_path: Path, dest_dir: Path) -> Path:
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / "catalog.db"
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        dest = sqlite3.connect(dest_path)
        try:
            src.backup(dest)
        finally:
            dest.close()
    finally:
        src.close()
    return dest_path


def _extract_rows(result: dict) -> list[tuple[str, float, bool]]:
    """The chips ``chip_ranking`` would show, in order: (name, probability, anchored)."""
    return [
        (str(row["name"]), float(row["probability"]), bool(row["anchored"]))
        for row in result["candidates"]
    ]


def evaluate(store, cases: list[Case], *, limit: int = 5, threshold: float = 0.90) -> dict:
    judge = SkillJudge(store)
    labelled_cases: list[Case] = []
    unlabelled_count = 0
    for c in cases:
        if c.expect is None:
            unlabelled_count += 1
        else:
            labelled_cases.append(c)

    case_rows: list[list[tuple[str, float, bool]]] = []
    case_ranks: list[int | None] = []

    for c in labelled_cases:
        res = chip_ranking(judge, c.draft, "", limit=limit)
        rows = _extract_rows(res)
        case_rows.append(rows)

        expect_set = {name.casefold() for name in c.expect} if c.expect else set()
        rank: int | None = None
        for idx, (name, _prob, _anch) in enumerate(rows, start=1):
            if name.casefold() in expect_set:
                rank = idx
                break
        case_ranks.append(rank)

    wanted = 0
    top1 = 0
    topk = 0
    none_cases = 0
    by_kind: dict[str, dict[str, int]] = {}
    misses: list[dict] = []

    for c, rank, rows in zip(labelled_cases, case_ranks, case_rows):
        if c.expect:
            wanted += 1
            if rank == 1:
                top1 += 1
            if rank is not None:
                topk += 1
            else:
                misses.append({
                    "draft": c.draft,
                    "expect": list(c.expect),
                    "got": [name for name, _p, _a in rows],
                    "kind": c.kind,
                })
            k = c.kind if c.kind != "" else "(none)"
            if k not in by_kind:
                by_kind[k] = {"wanted": 0, "top1": 0, "topk": 0}
            by_kind[k]["wanted"] += 1
            if rank == 1:
                by_kind[k]["top1"] += 1
            if rank is not None:
                by_kind[k]["topk"] += 1
        elif c.expect == ():
            none_cases += 1

    auto_fired = 0
    auto_right = 0
    auto_cases = 0
    none_auto = 0
    wrong_auto: list[dict] = []

    for c, rows in zip(labelled_cases, case_rows):
        expect_set = {name.casefold() for name in c.expect} if c.expect else set()
        auto_rows = [(name, prob, anch) for name, prob, anch in rows if prob >= threshold and anch]
        auto_fired += len(auto_rows)
        right_in_case = [name for name, _p, _a in auto_rows if name.casefold() in expect_set]
        auto_right += len(right_in_case)
        if c.expect:
            if right_in_case:
                auto_cases += 1
        elif c.expect == ():
            if auto_rows:
                none_auto += 1

        wrong_names = [name for name, _p, _a in auto_rows if name.casefold() not in expect_set]
        if wrong_names:
            wrong_auto.append({
                "draft": c.draft,
                "expect": list(c.expect),
                "auto": wrong_names,
            })

    sweep: list[dict] = []
    for th in THRESHOLDS:
        th_fired = 0
        th_right = 0
        th_cases = 0
        th_none_auto = 0
        for c, rows in zip(labelled_cases, case_rows):
            expect_set = {name.casefold() for name in c.expect} if c.expect else set()
            auto_rows = [(name, prob, anch) for name, prob, anch in rows if prob >= th and anch]
            th_fired += len(auto_rows)
            right_in_case = [name for name, _p, _a in auto_rows if name.casefold() in expect_set]
            th_right += len(right_in_case)
            if c.expect:
                if right_in_case:
                    th_cases += 1
            elif c.expect == ():
                if auto_rows:
                    th_none_auto += 1
        sweep.append({
            "threshold": th,
            "auto_fired": th_fired,
            "auto_right": th_right,
            "auto_cases": th_cases,
            "none_auto": th_none_auto,
        })

    return {
        "cases": len(labelled_cases),
        "unlabelled": unlabelled_count,
        "limit": limit,
        "threshold": threshold,
        "wanted": wanted,
        "top1": top1,
        "topk": topk,
        "auto_fired": auto_fired,
        "auto_right": auto_right,
        "auto_cases": auto_cases,
        "none_cases": none_cases,
        "none_auto": none_auto,
        "by_kind": by_kind,
        "sweep": sweep,
        "misses": misses,
        "wrong_auto": wrong_auto,
    }


def format_report(summary: dict, *, show_misses: bool = False) -> str:
    def _pct(num: int, denom: int) -> str:
        if denom == 0:
            return "n/a"
        return f"{num / denom * 100:.0f}%"

    cases = summary.get("cases", 0)
    unlabelled = summary.get("unlabelled", 0)
    limit = summary.get("limit", 5)
    threshold = summary.get("threshold", 0.90)
    wanted = summary.get("wanted", 0)
    top1 = summary.get("top1", 0)
    topk = summary.get("topk", 0)
    auto_fired = summary.get("auto_fired", 0)
    auto_right = summary.get("auto_right", 0)
    auto_cases = summary.get("auto_cases", 0)
    none_cases = summary.get("none_cases", 0)
    none_auto = summary.get("none_auto", 0)

    lines = [
        f"cases: {cases}, unlabelled: {unlabelled}",
        f"right skill first: {top1} of {wanted} ({_pct(top1, wanted)})",
        f"right skill in the top {limit}: {topk} of {wanted} ({_pct(topk, wanted)})",
        f"auto-attach at {threshold:.2f}: fired {auto_fired}, right {auto_right} ({_pct(auto_right, auto_fired)}), reached {auto_cases} of {wanted} wanted drafts",
        f"no-skill drafts: {none_cases}, auto-attached anyway: {none_auto}",
    ]

    for kind, data in summary.get("by_kind", {}).items():
        w = data.get("wanted", 0)
        t1 = data.get("top1", 0)
        tk = data.get("topk", 0)
        lines.append(f"{kind}: {t1} of {w} ({_pct(t1, w)}) first, {tk} of {w} ({_pct(tk, w)}) in top {limit}")

    for entry in summary.get("sweep", []):
        th = entry.get("threshold", 0.0)
        af = entry.get("auto_fired", 0)
        ar = entry.get("auto_right", 0)
        ac = entry.get("auto_cases", 0)
        na = entry.get("none_auto", 0)
        lines.append(
            f"threshold {th:.2f}: fired {af}, right {ar} ({_pct(ar, af)}), reached {ac} of {wanted} wanted drafts, no-skill auto: {na}"
        )

    if show_misses:
        for m in summary.get("misses", []):
            lines.append(f"miss: draft={m.get('draft')!r} expect={m.get('expect')} got={m.get('got')} kind={m.get('kind')}")
        for w in summary.get("wrong_auto", []):
            lines.append(f"wrong auto: draft={w.get('draft')!r} expect={w.get('expect')} auto={w.get('auto')}")

    return "\n".join(lines)


def collect(
    state_db: Path,
    out_path: Path,
    *,
    store=None,
    max_drafts: int = 200,
    hints: int = 5,
) -> int:
    state_db = Path(state_db)
    if not state_db.exists():
        return 0

    out_path = Path(out_path)
    seen_drafts: set[str] = set()
    if out_path.exists():
        try:
            for line in out_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    data = json.loads(line)
                    if isinstance(data, dict):
                        # "raw" keeps the collected text of a draft that was
                        # tidied by hand, so it is not collected again.
                        for d in (data.get("draft"), data.get("raw")):
                            if isinstance(d, str):
                                seen_drafts.add(d.strip().casefold())
                except Exception:
                    pass
        except Exception:
            pass

    conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=5)
    try:
        cur = conn.cursor()
        cur.execute("SELECT content, api_content FROM messages WHERE role = 'user' AND active = 1 ORDER BY id DESC")
        rows = cur.fetchall()
    finally:
        conn.close()

    new_cases: list[dict] = []
    judge = SkillJudge(store) if store is not None else None

    for content, api_content in rows:
        # ``content`` is what the user typed; ``api_content`` also carries what
        # plugins added for the model (router notes, expanded attachments).
        raw = content or api_content or ""
        text = strip_injected(raw)
        text = " ".join(text.split())
        text = text[:500]
        words = text.split()
        if len(words) < 3:
            continue
        folded = text.casefold()
        if folded in seen_drafts:
            continue
        seen_drafts.add(folded)

        if judge is not None:
            ranking_res = chip_ranking(judge, text, "", limit=hints)
            chip_rows = _extract_rows(ranking_res)
            hint_names = [name for name, _p, _a in chip_rows][:hints]
        else:
            hint_names = []

        new_cases.append({
            "draft": text,
            "expect": None,
            "kind": "",
            "hint": hint_names,
        })
        if len(new_cases) >= max_drafts:
            break

    if not new_cases:
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # A hand-edited file may lack its last newline; never join onto its last case.
    existing = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
    unterminated = bool(existing) and not existing.endswith("\n")
    with out_path.open("a", encoding="utf-8") as f:
        if unterminated:
            f.write("\n")
        for case_data in new_cases:
            f.write(json.dumps(case_data, ensure_ascii=False) + "\n")

    return len(new_cases)
