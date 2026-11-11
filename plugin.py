"""Hermes ``register(ctx)`` wiring. No core monkeypatches."""

from __future__ import annotations

import json
import logging

from .catalog_link import register_catalog_skills
from .cli import cli_handler, setup_cli
from .paths import default_db_path
from .service import Runtime, current_runtime, install_runtime
from .store import SkillStore
from .strip import strip_provider_request

logger = logging.getLogger("triage")


def register(ctx) -> None:
    """Wire the judge hook, the index strip, the skill_view guard, and setup commands."""
    try:
        store = SkillStore(default_db_path())
    except Exception:
        logger.exception("triage: catalog failed to open; hooks will no-op")
        store = None
    if store is not None:
        runtime = Runtime(store, ctx=ctx)
        install_runtime(runtime)
        _link_catalog(ctx, runtime)
        if runtime.settings().model_tools:
            from .model_tools import register_model_tools

            if register_model_tools(ctx):
                logger.info("triage: model tools skill_search / skill_load registered")

    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("pre_tool_call", pre_tool_call)
    ctx.register_hook("transform_tool_result", transform_tool_result)
    ctx.register_hook("post_compaction", post_compaction)
    ctx.register_middleware("llm_request", llm_request)
    ctx.register_middleware("tool_execution", tool_execution)

    def triage_command(raw_args: str, **kwargs) -> str:
        runtime = current_runtime()
        if runtime is None:
            return "triage is not loaded (catalog failed to open)."
        session_id = str(kwargs.get("session_id") or "")
        try:
            return runtime.handle_command(raw_args, session_id=session_id)
        except Exception as exc:
            logger.exception("triage command failed")
            return f"triage: {exc}"

    ctx.register_command(
        "triage",
        handler=triage_command,
        description="Rank plugin skills and load one probability-tagged choice",
        args_hint="[peek|use|find|list|sync|folders] <message>",
    )
    try:
        ctx.register_cli_command(
            name="triage",
            help="Import skills into triage and print the index cut",
            setup_fn=setup_cli,
            handler_fn=cli_handler,
            description="Plugin-owned skill catalog setup",
        )
    except Exception:
        logger.exception("triage: CLI command was not registered")


def _link_catalog(ctx, runtime) -> None:
    """Register catalog skills as /triage:<name> and sync skill folders in the background."""
    try:
        settings = runtime.settings()
        known = runtime.store.list_skills()
        if settings.register_slash:
            count = register_catalog_skills(ctx, known)
            logger.info("triage: registered %d catalog skills as /triage:<name>", count)
        from .watcher import start_folder_watcher

        # Live: new, changed and deleted skill folders are synced every 30 s,
        # and folders added in the settings are picked up without a restart.
        start_folder_watcher(
            ctx if settings.register_slash else None,
            runtime.store.db_path,
            lambda: list(runtime.settings().skill_folders),
            after_index=lambda store: _sync_hidden(runtime, store),
        )
    except Exception:
        logger.exception("triage: catalog link failed; chips still work")


def _sync_hidden(runtime, store) -> None:
    """Keep Hermes' skills.disabled in step with the Hermes-folder skills Triage serves."""
    from .hermes_hide import sync_hidden
    from .preload import parse_names

    settings = runtime.settings()
    line = sync_hidden(
        store,
        runtime.store.db_path,
        enabled=settings.hide_hermes_skills,
        keep_visible=set(parse_names(settings.always_load)),
    )
    if line:
        logger.info("triage: %s", line)


def pre_llm_call(**kwargs):
    runtime = current_runtime()
    if runtime is None:
        return None
    try:
        return runtime.on_pre_llm(**kwargs)
    except Exception:
        logger.exception("pre_llm_call failed open")
        return None


def pre_tool_call(**kwargs):
    runtime = current_runtime()
    if runtime is None:
        if str(kwargs.get("tool_name") or "") == "skill_view":
            return {
                "action": "block",
                "message": "triage: catalog is unavailable, so skill_view is blocked.",
            }
        return None
    try:
        return runtime.on_pre_tool(**kwargs)
    except Exception as exc:
        logger.exception("pre_tool_call failed closed")
        if str(kwargs.get("tool_name") or "") != "skill_view":
            return None
        return {
            "action": "block",
            "message": f"triage: guard error, skill_view blocked ({exc}).",
        }


def tool_execution(**kwargs):
    """Serve approved skill_view for catalog skills, and skill_manage for them.

    Catalog skills are not in Hermes' roots, so Hermes cannot resolve them;
    Triage answers. Anything else (a skill the model creates) goes to Hermes.
    """
    next_call = kwargs.get("next_call")
    runtime = current_runtime()
    tool_name = str(kwargs.get("tool_name") or "")
    raw_args = kwargs.get("args")
    args = raw_args if isinstance(raw_args, dict) else {}
    if runtime is None or next_call is None:
        return next_call() if next_call else ""
    turn_id = str(kwargs.get("turn_id") or kwargs.get("task_id") or "")
    session_id = str(kwargs.get("session_id") or "")
    try:
        if tool_name == "skill_view":
            served = runtime.serve_skill_view(args, turn_id=turn_id, session_id=session_id)
            if served is not None:
                return served
        elif tool_name == "skill_manage":
            served = runtime.serve_skill_manage(args, session_id=session_id)
            if served is not None:
                return served
            result = next_call()
            runtime.note_hermes_manage(args, session_id=session_id)
            return result
    except Exception as exc:
        logger.exception("triage tool_execution failed")
        return json.dumps({
            "success": False,
            "error": f"triage: {exc}",
            "served_by": "triage",
        })
    return next_call()


def transform_tool_result(**kwargs):
    """Replace a core 'skill not found' result when this session already approved the skill.

    ``tool_execution`` normally serves the call before Hermes runs. This hook
    covers a host that still executed the builtin and got an empty-root miss.
    """
    if str(kwargs.get("tool_name") or "") != "skill_view":
        return None
    runtime = current_runtime()
    if runtime is None:
        return None
    result = kwargs.get("result")
    if isinstance(result, str) and "triage" in result and "served_by" in result:
        return None
    try:
        return runtime.serve_skill_view(
            kwargs.get("args") if isinstance(kwargs.get("args"), dict) else {},
            turn_id=str(kwargs.get("turn_id") or ""),
            session_id=str(kwargs.get("session_id") or ""),
        )
    except Exception:
        logger.exception("transform_tool_result failed open")
        return None


def post_compaction(**kwargs):
    """Observer. The next pre_llm_call injects the unlocked-skills stub.

    Hermes ignores this hook's return value. See the post_compaction docs:
    re-inject from the following pre_llm_call.
    """
    runtime = current_runtime()
    if runtime is None:
        return None
    try:
        runtime.on_post_compaction(**kwargs)
    except Exception:
        logger.exception("post_compaction failed")
    return None


def llm_request(**kwargs):
    runtime = current_runtime()
    if runtime is not None and not runtime.settings().strip_skills_index:
        return None
    request = kwargs.get("request")
    if not isinstance(request, dict):
        return None
    try:
        updated, changed = strip_provider_request(request)
    except Exception:
        logger.exception("llm_request strip failed open")
        return None
    if not changed:
        return None
    return {
        "request": updated,
        "source": "triage",
        "reason": "strip skills index",
        "name": "strip_skills_index",
    }
