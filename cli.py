"""``hermes triage`` and ``python run.py`` setup commands.

Default ``migrate`` only copies SKILL.md trees into the plugin database.
``--apply-config`` and ``--empty-roots`` are separate, explicit steps.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

from .config_cut import cut_snippet, rewrite_skills_config, write_opt_out_marker
from .importer import import_tree
from .paths import default_config_path, default_data_dir, default_db_path, default_skills_dir, hermes_home
from .store import SkillStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes triage",
        description="Import skills into triage and cut the Hermes prompt index.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    migrate = sub.add_parser("migrate", help="Copy a SKILL.md tree into the plugin store")
    _add_db(migrate)
    migrate.add_argument("--from", dest="src", type=Path, default=None, help="Skills root (default: $HERMES_HOME/skills)")
    migrate.add_argument("--apply-config", action="store_true", help="Empty skills.auto_load and skills.external_dirs")
    migrate.add_argument("--config", type=Path, default=None, help="config.yaml to edit with --apply-config")
    migrate.add_argument(
        "--empty-roots",
        action="store_true",
        help="After a successful import, rename the source skills dir aside and write .no-bundled-skills",
    )
    migrate.add_argument("--yes", action="store_true", help="Required together with --empty-roots")

    listing = sub.add_parser("list", help="List skills in the plugin store")
    _add_db(listing)
    path_cmd = sub.add_parser("path", help="Print the catalog database path")
    _add_db(path_cmd)
    sub.add_parser("cut-snippet", help="Print the config.yaml fragment for the cut")

    peek = sub.add_parser("peek", help="Rank a message and print probability chips")
    _add_db(peek)
    peek.add_argument("query", nargs="+")

    restore = sub.add_parser(
        "restore",
        help="Give back every Hermes skill Triage hid (skills.disabled). Run before uninstalling Triage.",
    )
    _add_db(restore)

    status = sub.add_parser("status", help="Show store, marker, and skills-dir counts")
    _add_db(status)
    status.add_argument("--skills-dir", type=Path, default=None)

    evaluate = sub.add_parser("eval", help="Measure the chip ranking on labelled drafts")
    _add_db(evaluate)
    evaluate.add_argument("--cases", type=Path, default=None, help="JSONL of labelled drafts (default: <data dir>/eval/drafts.jsonl)")
    evaluate.add_argument("--limit", type=int, default=5)
    evaluate.add_argument("--threshold", type=float, default=0.90)
    evaluate.add_argument("--misses", action="store_true", help="Also list the missed drafts and wrong auto-attaches")
    evaluate.add_argument("--json", action="store_true", help="Print the summary as JSON")

    collect = sub.add_parser("eval-collect", help="Export real drafts from Hermes' history as unlabelled cases")
    _add_db(collect)
    collect.add_argument("--out", type=Path, default=None, help="JSONL to append to (default: <data dir>/eval/drafts.jsonl)")
    collect.add_argument("--max", dest="max_drafts", type=int, default=200)
    return parser


def _add_db(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", type=Path, default=None, help="Catalog sqlite path")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    db_path = Path(args.db) if getattr(args, "db", None) else default_db_path()
    if args.cmd == "path":
        print(db_path)
        return 0
    if args.cmd == "restore":
        from .hermes_hide import restore_all

        print(restore_all(db_path))
        print("Also set the plugin setting 'Hide Hermes' own skills' off, or Triage hides them again on its next start.")
        return 0
    if args.cmd == "cut-snippet":
        print(cut_snippet(), end="")
        return 0
    if args.cmd == "list":
        store = SkillStore(db_path)
        try:
            skills = store.list_skills()
        finally:
            store.close()
        if not skills:
            print(f"No skills in {db_path}")
            return 0
        for skill in skills:
            print(f"{skill.name}\t{skill.description}")
        return 0
    if args.cmd == "peek":
        from .judge import SkillJudge

        store = SkillStore(db_path)
        try:
            ranking = SkillJudge(store).rank(" ".join(args.query))
        finally:
            store.close()
        from .service import render_shortlist

        print(render_shortlist(ranking))
        return 0
    if args.cmd in ("eval", "eval-collect"):
        return _eval(args, db_path)
    if args.cmd == "status":
        return _status(db_path, args.skills_dir or default_skills_dir())
    if args.cmd == "migrate":
        return _migrate(args, db_path)
    parser.error(f"unknown command {args.cmd}")
    return 2


def setup_cli(subparser: argparse.ArgumentParser) -> None:
    """Argparse setup used by ``ctx.register_cli_command``."""
    subparser.add_argument(
        "args",
        nargs=argparse.REMAINDER,
        help="migrate | list | status | peek | eval | eval-collect | path | cut-snippet",
    )


def cli_handler(args) -> None:
    extra = list(getattr(args, "args", []) or [])
    code = main(extra)
    if code:
        raise SystemExit(code)


def _eval(args, db_path: Path) -> int:
    import json
    import tempfile
    from .context import state_db_path
    from .evalset import collect, evaluate, format_report, load_cases, snapshot

    default_path = default_data_dir() / "eval" / "drafts.jsonl"
    if not db_path.exists():
        print(f"No catalog at {db_path}")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="triage-eval-"))
    try:
        snapshot_path = snapshot(db_path, tmp)
        store = SkillStore(snapshot_path)
        try:
            if args.cmd == "eval":
                cases_path = Path(args.cases) if args.cases else default_path
                if not cases_path.exists():
                    print(f"No cases at {cases_path}. Run eval-collect, then label the drafts.")
                    return 1
                cases = load_cases(cases_path)
                summary = evaluate(store, cases, limit=args.limit, threshold=args.threshold)
                if args.json:
                    print(json.dumps(summary, indent=2, ensure_ascii=False))
                else:
                    print(format_report(summary, show_misses=args.misses))
                return 0
            if args.cmd == "eval-collect":
                out_path = Path(args.out) if args.out else default_path
                count = collect(state_db_path(), out_path, store=store, max_drafts=args.max_drafts)
                print(f"Added {count} drafts to {out_path}")
                return 0
        finally:
            store.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


def _migrate(args, db_path: Path) -> int:
    src = Path(args.src) if args.src else default_skills_dir()
    if args.empty_roots and not args.yes:
        print(
            "Refusing --empty-roots without --yes. "
            "Import first, read the printed checklist, then re-run with --empty-roots --yes.",
            file=sys.stderr,
        )
        return 2
    store = SkillStore(db_path)
    try:
        report = import_tree(store, src, copy=True)
    finally:
        store.close()
    print(report.summary())
    for line in report.skipped:
        print(f"skip: {line}", file=sys.stderr)
    for line in report.shadowed:
        print(f"shadow: {line}", file=sys.stderr)
    if report.skill_files == 0:
        print(
            "No SKILL.md files imported. Point --from at a skills tree "
            "(for a dry run: examples/demo-skills).",
            file=sys.stderr,
        )
        return 1

    if args.apply_config:
        config_path = Path(args.config) if args.config else default_config_path()
        _apply_config(config_path)
    else:
        print("Config left unchanged. Checklist:")
        print(cut_snippet(), end="")

    if args.empty_roots:
        _empty_roots(src)
    else:
        print(
            "Source skills directory left in place. "
            "Re-run with --empty-roots --yes after you have checked the import."
        )
    print(f"Catalog: {db_path}")
    return 0


def _apply_config(config_path: Path) -> None:
    original = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    result = rewrite_skills_config(original)
    if result.warning:
        print(result.warning, file=sys.stderr)
        snippet_path = config_path.with_name("triage-cut.yaml")
        snippet_path.write_text(cut_snippet(), encoding="utf-8")
        print(f"Wrote {snippet_path}")
        return
    if not result.changed:
        print(f"{config_path} already clears auto_load and external_dirs.")
        return
    if config_path.exists():
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        backup = config_path.with_name(config_path.name + f".bak-triage-{stamp}")
        shutil.copy2(config_path, backup)
        print(f"Backed up config to {backup}")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(result.text, encoding="utf-8")
    print(f"Updated {config_path}: skills.auto_load and skills.external_dirs are empty.")


def _empty_roots(src: Path) -> None:
    if not src.exists():
        print(f"Nothing to move at {src}")
        return
    if src.name != "skills":
        print(
            f"Refusing to move {src} because it is not named 'skills'. "
            "Move vendored project skills yourself.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    dest = src.with_name(f"skills.bak-triage-{stamp}")
    src.rename(dest)
    src.mkdir(parents=True, exist_ok=True)
    marker = write_opt_out_marker(src.parent)
    print(f"Moved {dest.name} aside and left {src} empty.")
    print(f"Wrote {marker}")
    print(
        "Hermes v0.22.7 still seeds ESSENTIAL_SKILLS (hermes-agent) on sync "
        "when that marker is present. Newer releases skip the seed. "
        "The llm_request middleware strips whatever <available_skills> block remains. "
        "Do not put names back into skills.auto_load."
    )
    home = hermes_home()
    if src.parent.resolve() != home.resolve():
        print(
            f"Marker written on {src.parent}, which is not HERMES_HOME ({home}). "
            "If this was a copy, also write the marker in the profile you actually run."
        )


def _status(db_path: Path, skills_dir: Path) -> int:
    store = SkillStore(db_path)
    try:
        count = store.count()
    finally:
        store.close()
    marker = hermes_home() / ".no-bundled-skills"
    skill_files = 0
    if skills_dir.exists():
        skill_files = sum(1 for _ in skills_dir.rglob("SKILL.md"))
    print(f"catalog: {db_path} ({count} skills)")
    print(f"skills dir: {skills_dir} ({skill_files} SKILL.md, exists={skills_dir.exists()})")
    print(f"opt-out marker: {marker} ({'present' if marker.exists() else 'absent'})")
    return 0
