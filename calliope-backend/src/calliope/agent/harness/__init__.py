"""Agent harness package.

Composition root: builds the shared ToolRegistry + SystemPromptService once,
loads all plugins, and installs the built-in policy hooks (destructive-action
guard). Modeled on the deepseek-harness plugin composition: the loop, the
orchestrator, and tests all program against these services, never against
individual plugins.
"""
from __future__ import annotations

import logging

from calliope.agent.harness.plugins import load_all as _load_plugins
from calliope.agent.harness.policy import (
    is_confirmation,
    is_render_request,
    user_allows_render,
)
from calliope.agent.harness.prompts import (
    SystemPromptService,
    register_builtin_sections,
)
from calliope.agent.harness.registry import (
    PreExecuteDecision,
    ToolContext,
    ToolDefinition,
    ToolRegistry,
    allow,
    deny,
)
from calliope.agent.harness.workflow_memory import (
    chosen_workflow,
    is_workflow_picker,
    user_asks_to_change_workflow,
)

logger = logging.getLogger("calliope.harness")

# Stable machine-readable refusal codes (consumed by the UI and tests — the
# human guidance text stays in the deny message for the model).
GUARD_RENDER_APPROVAL = "guard_render_approval"
GUARD_DESTRUCTIVE_REPLACE = "guard_destructive_replace"
# Agent shell (run_command) denials — defined here so the contracts exporter
# (which scans this module + registry.py) picks them up.
GUARD_SHELL_DISABLED = "guard_shell_disabled"
GUARD_SHELL_APPROVAL = "guard_shell_approval"
GUARD_SHELL_OUTSIDE_WORKSPACE = "guard_shell_outside_workspace"
GUARD_SHELL_DENIED_PATH = "guard_shell_denied_path"
GUARD_SHELL_DENIED_COMMAND = "guard_shell_denied_command"
# Scene tool scope guard lives in registry.py (circular-import free); the
# contracts exporter reads guard codes from THIS module, so re-export here.
from calliope.agent.harness.registry import (  # noqa: E402
    GUARD_SCENE_TOOL_SCOPE,
    GUARD_NO_EXPORT_VIDEO_TOOL,
    GUARD_WORKSPACE_PATH,
    EXPORT_VIDEO_DENIED_NAMES,
    _scene_scoped,
)

# Re-exports for tests / older imports.
_is_confirmation = is_confirmation
_is_render_request = is_render_request


def _user_confirmed_replacement(ctx: ToolContext) -> bool:
    """True when the user's latest input explicitly asks for / confirms a
    destructive replace — a scoped question-card answer or prose (derived
    from the event log — no separate state)."""
    from calliope.agent.harness import log as session_log
    from calliope.agent.harness.policy import has_structured_approval

    if has_structured_approval(ctx, "destructive_replace"):
        return True
    from calliope.agent.harness.workflow_memory import message_confirms_replacement

    latest = session_log.latest_user_message(ctx.session_id) or ""
    return message_confirms_replacement(latest)


def _render_approval_guard(ctx: ToolContext, t: ToolDefinition, args: dict) -> PreExecuteDecision:
    """Human-in-the-loop guard for image/video generation.

    Rendering (enqueue_asset_jobs / enqueue_video_jobs / run_workflow)
    queues real, expensive, side-effecting work. It runs when the user asked
    for generation or confirmed it. A later complaint does not cancel that
    yes. A new task ("add a character") or a cancel does. Text edits do NOT
    grant render permission — this is what previously let the assets-agent
    auto-generate images after a "create a Misc. Item" request.
    """
    if not t.requires_approval:
        return allow()
    if user_allows_render(ctx):
        return allow()
    return deny(
        "Image/video generation is human-in-the-loop and needs explicit user approval. "
        "The user's latest message does not ask to render. Complete the requested text "
        "edits, then ASK whether they want images/videos generated — do not enqueue "
        "until they confirm (e.g. 'yes, generate the images').",
        code=GUARD_RENDER_APPROVAL,
    )


def _workflow_reask_guard(ctx: ToolContext, t: ToolDefinition, args: dict) -> PreExecuteDecision:
    """Stop a second 'which workflow?' card after the user already picked one.

    Yes/no generate confirms still pass. A message that asks to switch
    workflows unlocks a new menu.
    """
    if t.name != "ask_user":
        return allow()
    chosen = chosen_workflow(ctx.session_id)
    if not chosen:
        return allow()
    from calliope.agent.harness import log as session_log

    latest = session_log.latest_user_message(ctx.session_id) or ""
    if user_asks_to_change_workflow(latest):
        return allow()
    question = str(args.get("question") or "")
    raw_options = args.get("options") or []
    options = [str(opt) for opt in raw_options] if isinstance(raw_options, list) else []
    if not is_workflow_picker(question, options):
        return allow()
    return deny(
        f"The user already chose workflow_id={chosen['id']} ({chosen['name']}). "
        "Do not ask which workflow. Call run_workflow or enqueue_asset_jobs "
        f"with workflow_id={chosen['id']}."
    )


def _destructive_guard(ctx: ToolContext, t: ToolDefinition, args: dict) -> PreExecuteDecision:
    """Block silent destructive *regeneration* unless the user asked for it.

    Scope: tools with a `replace` parameter (generate_story / generate_script)
    — "replace=true (default) deletes everything first". A destructive tool
    WITHOUT a replace param (delete_scene, comfy lifecycle tools) acts on one
    explicit target the model named, so it is not blocked here.

    A non-empty project is protected unless the user's latest message is an
    explicit confirmation / regeneration request (e.g. "yes, replace it").
    That turns "ask the user first" from a dead-end into a real confirm step.
    """
    if not t.destructive:
        return allow()
    has_replace_param = "replace" in t.parameters.get("properties", {})
    if not has_replace_param:
        return allow()
    replace = bool(args.get("replace", True))
    # BOTH modes are gated on a non-empty project: replace=true deletes the
    # existing content, and replace=false APPENDS a second full set alongside
    # it (observed live 2026-08-25: an unconfirmed replace=false generate_story
    # silently doubled a project's beats, 25 -> 50). Only allow when the
    # project is effectively empty, or the user explicitly confirmed.
    from calliope.agent.harness.registry import _db

    if ctx.project_id is None:
        return allow()
    conn = _db()
    try:
        scenes = conn.execute(
            "SELECT COUNT(*) AS n FROM scenes WHERE project_id = ?", (ctx.project_id,)
        ).fetchone()["n"]
        beats = conn.execute(
            "SELECT COUNT(*) AS n FROM story_beats WHERE project_id = ?", (ctx.project_id,)
        ).fetchone()["n"]
        chars = conn.execute(
            "SELECT COUNT(*) AS n FROM characters WHERE project_id = ?", (ctx.project_id,)
        ).fetchone()["n"]
        locs = conn.execute(
            "SELECT COUNT(*) AS n FROM locations WHERE project_id = ?", (ctx.project_id,)
        ).fetchone()["n"]
        items = conn.execute(
            "SELECT COUNT(*) AS n FROM items WHERE project_id = ?", (ctx.project_id,)
        ).fetchone()["n"]
    finally:
        conn.close()
    # generate_script deletes or appends SCENES only. Beats and characters
    # stay. The first script on a project that has a story but zero scenes
    # is the user's "create scenes" request, not a rewrite.
    if t.name == "generate_script" and scenes == 0:
        return allow()
    has_content = (scenes + beats + chars + locs + items) > 0
    if not has_content:
        return allow()
    if _user_confirmed_replacement(ctx):
        return allow()
    counts = (
        f"Project #{ctx.project_id} already has content ({scenes} scenes, {beats} beats, "
        f"{chars} characters, {locs} locations, {items} items)."
    )
    if t.name == "generate_script":
        action = "replace the existing scenes" if replace else "append a second script"
        return deny(
            counts + f" generate_script would {action}. "
            "Beats and characters are not touched. "
            "Call it only after the user asks to recreate the scenes, or after "
            "ask_user with scope='destructive_replace' is answered yes.",
            code=GUARD_DESTRUCTIVE_REPLACE,
        )
    if replace:
        return deny(
            counts + " replace=true DELETES all of it first, and the user's "
            "latest message did not ask to replace the story or script. "
            "A yes to generating images does not allow this. "
            "For targeted changes use the granular tools (add/update/delete). "
            "Call generate_story only after they ask to rewrite it, or after "
            "ask_user with scope='destructive_replace' is answered yes. "
            "Do not retry replace=true before that.",
            code=GUARD_DESTRUCTIVE_REPLACE,
        )
    return deny(
        counts + " replace=false would APPEND a second full set alongside "
        "the existing content (e.g. doubling the beat list). "
        "A yes to generating images does not allow this. "
        "Use the granular tools (add/update/delete) instead. "
        "Do not retry either mode unless they ask to rewrite the story or "
        "script, or answer a scope='destructive_replace' question.",
        code=GUARD_DESTRUCTIVE_REPLACE,
    )


_BUILT = False
_registry: ToolRegistry | None = None
_prompts: SystemPromptService | None = None


def build_harness() -> tuple[ToolRegistry, SystemPromptService]:
    """Compose the harness (idempotent). Returns (registry, prompts)."""
    global _BUILT, _registry, _prompts
    if _BUILT:
        assert _registry is not None and _prompts is not None
        return _registry, _prompts
    registry = ToolRegistry()
    prompts = SystemPromptService()
    _load_plugins()
    from calliope.agent.harness.plugins import (  # noqa: E402
        canvas,
        files,
        interaction,
        memory,
        render,
        script,
        shot_builder,
        skills,
        story,
        system,
        workspace,
    )

    workspace.register(registry)
    story.register(registry)
    script.register(registry)
    render.register(registry)
    canvas.register(registry)
    interaction.register(registry)
    memory.register(registry)
    skills.register(registry)
    shot_builder.register(registry)
    system.register(registry)
    files.register(registry)
    registry.on_pre_execute(_destructive_guard)
    registry.on_pre_execute(_render_approval_guard)
    registry.on_pre_execute(_workflow_reask_guard)
    register_builtin_sections(prompts)
    _registry, _prompts = registry, prompts
    _BUILT = True
    logger.info("Harness composed: %d tools", len(registry.tools))
    return registry, prompts


def get_registry() -> ToolRegistry:
    registry, _ = build_harness()
    return registry


def get_prompts() -> SystemPromptService:
    _, prompts = build_harness()
    return prompts
