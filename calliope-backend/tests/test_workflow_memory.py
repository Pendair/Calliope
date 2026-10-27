"""The session remembers a workflow the user already picked.

Session 974 asked "which workflow?" three times: each card answer started a
new swarm whose planner only saw that one line, and the assets sub-agent
started with an empty history.
"""
from __future__ import annotations

import asyncio
import json

from calliope.agent.harness import log as session_log
from calliope.agent.harness.registry import ToolContext
from calliope.agent.harness.tools import execute_tool
from calliope.agent.harness.workflow_memory import (
    chosen_workflow,
    is_workflow_picker,
    match_workflow,
    message_asks_for_script,
)
from calliope.config import settings
from calliope.db import get_db


def _workflow(name: str) -> int:
    conn = get_db(settings.db_path)
    try:
        cur = conn.execute(
            """
            INSERT INTO workflows
                (name, kind, workflow_json, input_schema, output_schema, is_enabled)
            VALUES (?, 'image', '{}', '[]', '[]', 1)
            """,
            (name,),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _session(client) -> int:
    resp = client.post("/api/agent/sessions", json={})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def test_a_complaint_does_not_cancel_an_image_approval(client):
    from calliope.agent.harness.policy import user_allows_render

    sid = _session(client)
    session_log.append_event(
        sid, session_log.USER_MESSAGE, {"content": "Yes, generate all 4 now"}
    )
    session_log.append_event(
        sid,
        session_log.USER_MESSAGE,
        {"content": "your fucking reasoning is suck like shit!"},
    )
    assert user_allows_render(ToolContext(session_id=sid, project_id=1)) is True

    session_log.append_event(
        sid, session_log.USER_MESSAGE, {"content": "add a character named Mei"}
    )
    assert user_allows_render(ToolContext(session_id=sid, project_id=1)) is False


def test_an_image_yes_does_not_rewrite_the_story(client):
    from calliope.agent.harness import build_harness

    registry, _ = build_harness()
    pid = client.post("/api/projects", json={"title": "Stay"}).json()["id"]
    created = client.post(
        f"/api/projects/{pid}/scenes", json={"heading": "S1", "order_index": 1}
    )
    assert created.status_code in (200, 201), created.text
    sid = _session(client)
    session_log.append_event(
        sid, session_log.USER_MESSAGE, {"content": "Yes, generate all 4 now"}
    )
    out = asyncio.run(
        registry.execute(
            ToolContext(session_id=sid, project_id=pid),
            "generate_story",
            {"replace": True},
        )
    )
    assert out["ok"] is False
    assert "blocked:" in out["error"]
    assert "images" in out["error"]


def test_a_complaint_is_not_a_script_request():
    assert message_asks_for_script("Build the whole film from scratch")
    assert message_asks_for_script("write the script and break it into shots")
    assert not message_asks_for_script(
        "and now you are reasoning to generate script?!!  "
        "have I asked you that before?  NO , so why the fuck you do that?"
    )
    assert not message_asks_for_script(
        "you keep asking which workflow, keep reasoning what character, "
        "and image prompt!!!! but you are not working!!!!"
    )


def test_match_requires_the_name_not_a_bare_number():
    rows = [{"id": 35, "name": "krea2_T2I_CharSheetV1_API"}]
    assert match_workflow("35", rows) is None
    assert match_workflow("generate 35 images", rows) is None
    assert match_workflow("35 krea2_T2I_CharSheetV1_API", rows)["id"] == 35
    assert match_workflow("krea2_T2I_CharSheetV1_API (id 35)", rows)["id"] == 35


def test_yes_does_not_forget_the_workflow(client):
    sid = _session(client)
    wid = _workflow("krea2_T2I_CharSheetV1_API")
    session_log.append_event(
        sid,
        session_log.QUESTION_ANSWERED,
        {"answer": "krea2_T2I_CharSheetV1_API (35)", "scope": "render"},
    )
    session_log.append_event(
        sid, session_log.USER_MESSAGE, {"content": "krea2_T2I_CharSheetV1_API (35)"}
    )
    session_log.append_event(
        sid, session_log.USER_MESSAGE, {"content": "Yes, generate the 4 images"}
    )
    chosen = chosen_workflow(sid)
    assert chosen is not None
    assert chosen["id"] == wid
    assert chosen["name"] == "krea2_T2I_CharSheetV1_API"


def test_a_later_name_replaces_the_choice(client):
    sid = _session(client)
    _workflow("krea2_T2I_CharSheetV1_API")
    other = _workflow("QwenImg_2-1_t2i-charactersheet")
    session_log.append_event(
        sid, session_log.USER_MESSAGE, {"content": "krea2_T2I_CharSheetV1_API (35)"}
    )
    session_log.append_event(
        sid,
        session_log.USER_MESSAGE,
        {"content": "use QwenImg_2-1_t2i-charactersheet instead"},
    )
    assert chosen_workflow(sid)["id"] == other


def test_workflow_menu_is_a_picker_and_a_yes_no_is_not():
    rows = [
        {"id": 35, "name": "krea2_T2I_CharSheetV1_API"},
        {"id": 39, "name": "Krea2_t2i_20260818_API"},
    ]
    assert is_workflow_picker(
        "Which image workflow should I use?",
        ["krea2_T2I_CharSheetV1_API (35)", "Krea2_t2i_20260818_API (39)"],
        rows,
    )
    assert not is_workflow_picker(
        "Generate the 4 character images now?",
        ["Yes, generate all 4 now", "No, not yet"],
        rows,
    )


def test_repeat_workflow_question_is_blocked(client):
    sid = _session(client)
    wid = _workflow("krea2_T2I_CharSheetV1_API")
    _workflow("Krea2_t2i_20260818_API")
    session_log.append_event(
        sid, session_log.USER_MESSAGE, {"content": "Yes, generate the 4 images"}
    )
    session_log.append_event(
        sid,
        session_log.QUESTION_ANSWERED,
        {"answer": f"krea2_T2I_CharSheetV1_API (id {wid})", "scope": "render"},
    )
    # Newest user line does not name the workflow; the earlier answer does.
    # Re-order to match the live session: the pick, then the yes.
    # The yes was appended first above — add the pick BEFORE it by inserting
    # the answered event is already after the yes, so the pick is newest and
    # still wins. Flip to the live order explicitly:
    session_log.append_event(
        sid, session_log.USER_MESSAGE, {"content": "Yes, generate the 4 images"}
    )
    result = asyncio.run(
        execute_tool(
            ToolContext(session_id=sid, project_id=1),
            "ask_user",
            {
                "question": "Which image workflow should I use for the 4 character portraits?",
                "options": [
                    "krea2_T2I_CharSheetV1_API (id 35)",
                    "Krea2_t2i_20260818_API (id 39)",
                ],
                "scope": "render",
            },
        )
    )
    assert result["ok"] is False
    assert f"workflow_id={wid}" in result["error"]
    assert not any(e.type == session_log.QUESTION_ASKED for e in session_log.read_events(sid))


def test_generate_confirm_still_asks(client):
    sid = _session(client)
    _workflow("krea2_T2I_CharSheetV1_API")
    session_log.append_event(
        sid, session_log.USER_MESSAGE, {"content": "krea2_T2I_CharSheetV1_API"}
    )
    result = asyncio.run(
        execute_tool(
            ToolContext(session_id=sid, project_id=1),
            "ask_user",
            {
                "question": "Generate the 4 character images now?",
                "options": ["Yes, generate all 4 now", "No, not yet"],
                "scope": "render",
            },
        )
    )
    assert result["ok"] is True
    assert result["awaiting_user_input"] is True


def test_switch_workflow_unlocks_the_menu(client):
    sid = _session(client)
    _workflow("krea2_T2I_CharSheetV1_API")
    _workflow("Krea2_t2i_20260818_API")
    session_log.append_event(
        sid, session_log.USER_MESSAGE, {"content": "krea2_T2I_CharSheetV1_API"}
    )
    session_log.append_event(
        sid, session_log.USER_MESSAGE, {"content": "switch workflow"}
    )
    result = asyncio.run(
        execute_tool(
            ToolContext(session_id=sid, project_id=1),
            "ask_user",
            {
                "question": "Which image workflow should I use?",
                "options": [
                    "krea2_T2I_CharSheetV1_API",
                    "Krea2_t2i_20260818_API",
                ],
                "scope": "render",
            },
        )
    )
    assert result["ok"] is True


def test_planner_and_assets_task_are_told_the_choice(client):
    import calliope.agent.harness.orchestrator as orch

    sid = _session(client)
    wid = _workflow("krea2_T2I_CharSheetV1_API")
    session_log.append_event(
        sid,
        session_log.USER_MESSAGE,
        {"content": f"{wid} krea2_T2I_CharSheetV1_API"},
    )
    session_log.append_event(
        sid, session_log.USER_MESSAGE, {"content": "Yes, generate the 4 images"}
    )
    seen: dict[str, str] = {}

    class _Planner:
        async def chat_stream(self, messages, *a, **kw):
            seen["planner"] = messages[-1]["content"]
            yield {
                "type": "delta",
                "content": json.dumps(
                    {
                        "mode": "swarm",
                        "note": "one assets task",
                        "tasks": [
                            {"role": "assets", "goal": "generate the character sheets"},
                            {"role": "story", "goal": "leave the story alone"},
                        ],
                    }
                ),
            }
            yield {"type": "done"}

        async def close(self):
            return None

    captured: list[str] = []

    async def fake_run_sub(ctx, sub_history, allowed, *, agent_name=None, on_message=None):
        captured.append(sub_history[0]["content"])
        return "done"

    orig_client = orch.LLMClient
    orig_run_sub = orch._run_sub_agent
    orig_run_turn = orch.run_turn
    orig_ws = orch.get_registry
    orch.LLMClient = lambda: _Planner()
    orch._run_sub_agent = fake_run_sub
    orch.run_turn = lambda ctx, history, *, on_message=None: _done()

    class _Workspace:
        async def execute(self, ctx, name, args):
            return {"ok": True, "characters": []}

    orch.get_registry = lambda: _Workspace()
    try:
        ctx = ToolContext(session_id=sid, project_id=1)
        asyncio.run(orch.orchestrate(ctx, [], session_id=sid))
    finally:
        orch.LLMClient = orig_client
        orch._run_sub_agent = orig_run_sub
        orch.run_turn = orig_run_turn
        orch.get_registry = orig_ws

    assert f"workflow_id={wid}" in seen["planner"]
    assert "not a count" in seen["planner"]
    assert "Yes, generate the 4 images" in seen["planner"]
    assert len(captured) == 1
    assert "assets sub-agent" in captured[0]
    assert f"workflow_id={wid}" in captured[0]
    assert "run_workflow" in captured[0]
    assert "story sub-agent" not in captured[0]
    notes = [
        e.data.get("content") or ""
        for e in session_log.read_events(sid)
        if e.type == session_log.ASSISTANT_MESSAGE and e.data.get("agent_name") == "planner"
    ]
    assert any("No script" in n for n in notes)


async def _done():
    return "single"


def test_story_rewrite_is_dropped_and_the_conversation_is_kept(client):
    import calliope.agent.harness.orchestrator as orch

    sid = _session(client)
    session_log.append_event(
        sid,
        session_log.USER_MESSAGE,
        {"content": "The outline is already in the project. Leave the story alone."},
    )
    session_log.append_event(
        sid,
        session_log.USER_MESSAGE,
        {"content": "generate the character images. do not touch the story"},
    )
    captured: list[str] = []

    class _Planner:
        async def chat_stream(self, messages, *a, **kw):
            yield {
                "type": "delta",
                "content": json.dumps(
                    {
                        "mode": "swarm",
                        "note": "Regenerating the storyline, then the images.",
                        "tasks": [
                            {
                                "role": "story",
                                "goal": "Call generate_story to regenerate the storyline.",
                            },
                            {"role": "assets", "goal": "Prepare the character images."},
                        ],
                    }
                ),
            }
            yield {"type": "done"}

        async def close(self):
            return None

    async def fake_run_sub(ctx, sub_history, allowed, *, agent_name=None, on_message=None):
        captured.append(sub_history[0]["content"])
        return "done"

    orig = (orch.LLMClient, orch._run_sub_agent, orch.run_turn, orch.get_registry)
    orch.LLMClient = lambda: _Planner()
    orch._run_sub_agent = fake_run_sub
    orch.run_turn = lambda ctx, history, *, on_message=None: _done()

    class _Workspace:
        async def execute(self, ctx, name, args):
            return {"ok": True}

    orch.get_registry = lambda: _Workspace()
    try:
        ctx = ToolContext(session_id=sid, project_id=1)
        asyncio.run(orch.orchestrate(ctx, [], session_id=sid))
    finally:
        orch.LLMClient, orch._run_sub_agent, orch.run_turn, orch.get_registry = orig

    assert len(captured) == 1
    assert "assets sub-agent" in captured[0]
    assert "generate_story" not in captured[0]
    assert "Leave the story alone" in captured[0]
    assert "CONVERSATION SO FAR" in captured[0]
    plan = next(e for e in session_log.read_events(sid) if e.type == session_log.PLAN_CREATED)
    assert [t["role"] for t in plan.data["tasks"]] == ["assets"]


def test_not_working_does_not_schedule_a_script(client):
    """Session 974: an angry 'you are not working' after a yes was planned as
    generate_script. The approved job was the character images."""
    import calliope.agent.harness.orchestrator as orch

    sid = _session(client)
    wid = _workflow("krea2_T2I_CharSheetV1_API")
    session_log.append_event(
        sid, session_log.USER_MESSAGE, {"content": f"{wid} krea2_T2I_CharSheetV1_API"}
    )
    session_log.append_event(
        sid, session_log.USER_MESSAGE, {"content": "Yes, generate all 4 now"}
    )
    session_log.append_event(
        sid,
        session_log.USER_MESSAGE,
        {
            "content": (
                "fuck you ! you keep asking which workflow, keep reasoning "
                "what character, and image prompt!!!! but you are not working!!!!"
            )
        },
    )
    captured: list[str] = []

    class _Planner:
        async def chat_stream(self, messages, *a, **kw):
            yield {
                "type": "delta",
                "content": json.dumps(
                    {
                        "mode": "swarm",
                        "note": (
                            "Skipping the questions — building the full scene "
                            "list from your beats first."
                        ),
                        "tasks": [
                            {
                                "role": "script",
                                "goal": (
                                    "call generate_script then break_into_shots. "
                                    "Do not ask the user anything."
                                ),
                            },
                            {
                                "role": "assets",
                                "goal": "Write image prompts. Do not ask the user.",
                            },
                        ],
                    }
                ),
            }
            yield {"type": "done"}

        async def close(self):
            return None

    async def fake_run_sub(ctx, sub_history, allowed, *, agent_name=None, on_message=None):
        captured.append(sub_history[0]["content"])
        return "done"

    orig = (orch.LLMClient, orch._run_sub_agent, orch.run_turn, orch.get_registry)
    orch.LLMClient = lambda: _Planner()
    orch._run_sub_agent = fake_run_sub
    orch.run_turn = lambda ctx, history, *, on_message=None: _done()

    class _Workspace:
        async def execute(self, ctx, name, args):
            return {"ok": True}

    orch.get_registry = lambda: _Workspace()
    try:
        ctx = ToolContext(session_id=sid, project_id=1)
        asyncio.run(orch.orchestrate(ctx, [], session_id=sid))
    finally:
        orch.LLMClient, orch._run_sub_agent, orch.run_turn, orch.get_registry = orig

    assert len(captured) == 1
    assert "script sub-agent" not in captured[0]
    assert "generate_script" not in captured[0]
    assert f"workflow_id={wid}" in captured[0]
    plan = next(
        e for e in session_log.read_events(sid) if e.type == session_log.PLAN_CREATED
    )
    assert [t["role"] for t in plan.data["tasks"]] == ["assets"]
    notes = [
        e.data.get("content") or ""
        for e in session_log.read_events(sid)
        if e.type == session_log.ASSISTANT_MESSAGE
    ]
    assert not any("scene list" in n for n in notes)
    assert any("No script" in n for n in notes)


def test_existing_story_is_told_to_the_planner_before_it_thinks(client):
    import calliope.agent.harness.orchestrator as orch

    sid = _session(client)
    session_log.append_event(
        sid, session_log.USER_MESSAGE, {"content": "generate the character images"}
    )
    seen: dict[str, str] = {}

    class _Planner:
        async def chat_stream(self, messages, *a, **kw):
            seen["planner"] = messages[-1]["content"]
            yield {
                "type": "delta",
                "content": json.dumps({"mode": "single", "tasks": [], "note": ""}),
            }
            yield {"type": "done"}

        async def close(self):
            return None

    orig = (orch.LLMClient, orch.run_turn, orch.get_registry)
    orch.LLMClient = lambda: _Planner()
    orch.run_turn = lambda ctx, history, *, on_message=None: _done()

    class _Workspace:
        async def execute(self, ctx, name, args):
            return {"ok": True, "stats": {"character_count": 4, "scene_count": 0}}

    orch.get_registry = lambda: _Workspace()
    try:
        ctx = ToolContext(session_id=sid, project_id=1)
        asyncio.run(orch.orchestrate(ctx, [], session_id=sid))
    finally:
        orch.LLMClient, orch.run_turn, orch.get_registry = orig

    assert "EXISTING STORY" in seen["planner"]
    assert "generate_story" in seen["planner"]
    assert "generate_script" in seen["planner"]
