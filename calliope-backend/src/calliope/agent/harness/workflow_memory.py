"""Remember the workflow the user already picked in this session.

Swarm sub-agents start from a blank history, and the planner only sees the
latest user line. A card answer of "krea2_T2I_CharSheetV1_API (35)" followed
by "Yes, generate the 4 images" was planned as a brand-new request: the
number 35 was read as a sheet count, and the assets agent called ask_user
to pick a workflow again. This module is the session fact those turns were
missing.
"""
from __future__ import annotations

import re
from typing import Any

from calliope.agent.harness import log as session_log
from calliope.agent.harness.policy import _cue_is_negated, is_confirmation, is_render_request
from calliope.config import settings
from calliope.db import get_db

# "switch workflow" / "換工作流" unlocks a new picker. "which workflow" does
# not — that phrase is what the user says when the agent asks again.
_CHANGE_WORKFLOW_RE = re.compile(
    r"(different|another|switch|change|other)\W{0,24}workflow"
    r"|換\s*(一個)?\s*(工作流|workflow)"
    r"|另一個\s*(工作流|workflow)"
    r"|別的\s*(工作流|workflow)",
    re.IGNORECASE,
)
_WHICH_WORKFLOW_RE = re.compile(
    r"which\s+(image\s+|video\s+)?workflow"
    r"|哪個\s*(image\s*)?(workflow|工作流)"
    r"|要用哪個",
    re.IGNORECASE,
)
_EXPLICIT_ID_RE = re.compile(
    r"workflow_id\s*[:=]\s*(\d+)|(?<![A-Za-z])id\s*[:=]?\s*(\d+)",
    re.IGNORECASE,
)
_MIN_NAME_LEN = 4
# A script/scene task is allowed when the user asked for one. "film" counts
# ("build the whole film"). A complaint that names the word script
# ("have I asked you to generate script? NO") does not.
_SCRIPT_CUE_RE = re.compile(
    r"\b(script|screenplay|scenes?|shots?|film|movie)\b|劇本|分場|分鏡",
    re.IGNORECASE,
)
_SCRIPT_REFUSAL_RE = re.compile(
    r"have i asked|did i ask|i (?:did not|didn't|never) ask"
    r"|why\b.{0,80}\b(?:script|scene|shot|story|regenerat)"
    r"|沒叫|沒有叫|沒要你|不要寫|別寫",
    re.IGNORECASE,
)
# A picture-generation "yes" is not one of these. "yes, replace everything"
# and "regenerate the script from scratch" are.
_REPLACE_CUE_RE = re.compile(
    r"\b(replace|append|regenerat\w*|rewrit\w*|overwrite|wipe|redo|start\s+over|from\s+scratch)\b"
    r"|重寫|重写|重新生成|覆寫",
    re.IGNORECASE,
)
_OTHER_WORK_RE = re.compile(
    r"\b(story|beats?|script|screenplay|scenes?|shots?|video|clips?)\b|劇本|分場|分鏡",
    re.IGNORECASE,
)
_GENERATE_RE = re.compile(
    r"\b(generat\w*|render\w*|image|images|portrait|sheet)\b",
    re.IGNORECASE,
)
# A complaint is not a new task. "add a character" is, and must not inherit
# an earlier image approval.
_NEW_TASK_RE = re.compile(
    r"\b(add|update|write|create|change|edit|delete|remove|fix|make|draft|rename)\b",
    re.IGNORECASE,
)
_REVOKE_RENDER_RE = re.compile(
    r"\b(don't|do not|never|stop|cancel|abort)\b.{0,40}"
    r"\b(generat\w*|render\w*|image|images|workflow)\b"
    r"|^\s*(no|nope|stop|cancel|abort)\b",
    re.IGNORECASE,
)
_SCRIPT_GOAL_RE = re.compile(
    r"generate_script|break_into_shots|add_scene|scene list|full scene",
    re.IGNORECASE,
)


def _enabled_workflows() -> list[dict[str, Any]]:
    conn = get_db(settings.db_path)
    try:
        rows = conn.execute(
            "SELECT id, name FROM workflows WHERE is_enabled = 1"
        ).fetchall()
        return [{"id": int(r["id"]), "name": str(r["name"] or "")} for r in rows]
    finally:
        conn.close()


def match_workflow(text: str, workflows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The workflow named in `text`, or None.

    A full enabled name (longest wins) is the match. `workflow_id=N` / `id N`
    counts only when that id is enabled. A bare number, including one inside
    parentheses, is not a choice — "35" on its own was being read as a count.
    """
    folded = (text or "").casefold()
    if not folded.strip():
        return None
    by_name = [
        w
        for w in workflows
        if len(w["name"]) >= _MIN_NAME_LEN and w["name"].casefold() in folded
    ]
    if by_name:
        by_name.sort(key=lambda w: len(w["name"]), reverse=True)
        return by_name[0]
    by_id = {w["id"]: w for w in workflows}
    found: list[dict[str, Any]] = []
    for m in _EXPLICIT_ID_RE.finditer(text or ""):
        raw = m.group(1) or m.group(2)
        hit = by_id.get(int(raw))
        if hit and hit not in found:
            found.append(hit)
    if len(found) == 1:
        return found[0]
    return None


def _mention_workflow(
    data: dict[str, Any], by_id: dict[int, dict[str, Any]]
) -> dict[str, Any] | None:
    mentions = data.get("mentions")
    if not isinstance(mentions, list):
        return None
    for m in mentions:
        if not isinstance(m, dict) or m.get("type") not in (None, "workflow"):
            continue
        try:
            wid = int(m["id"])
        except (KeyError, TypeError, ValueError):
            continue
        hit = by_id.get(wid)
        if hit:
            return hit
    return None


def chosen_workflow(session_id: int) -> dict[str, Any] | None:
    """Newest user pick of an enabled workflow, or None.

    Later messages that do not name a workflow leave the choice in place.
    Steering does not count. A newer name or @workflow tag replaces it.
    """
    workflows = _enabled_workflows()
    if not workflows:
        return None
    by_id = {w["id"]: w for w in workflows}
    events = session_log.read_events(session_id)
    for ev in reversed(events):
        data = ev.data if isinstance(ev.data, dict) else {}
        if ev.type == session_log.USER_MESSAGE:
            tagged = _mention_workflow(data, by_id)
            if tagged:
                return tagged
            hit = match_workflow(str(data.get("content") or ""), workflows)
            if hit:
                return hit
        elif ev.type == session_log.QUESTION_ANSWERED:
            hit = match_workflow(str(data.get("answer") or ""), workflows)
            if hit:
                return hit
    return None


def format_choice(chosen: dict[str, Any]) -> str:
    wid = chosen["id"]
    name = chosen["name"]
    return (
        f"CHOSEN WORKFLOW (the user already picked this — binding): "
        f"workflow_id={wid} name={name}. "
        f"{wid} is the workflow id, not a count of images, sheets, or assets. "
        f"Pass workflow_id={wid} to run_workflow or enqueue_asset_jobs. "
        f"Do not ask which workflow, and do not ask what the number means."
    )


def user_asks_to_change_workflow(text: str) -> bool:
    return bool(_CHANGE_WORKFLOW_RE.search(text or ""))


def is_workflow_picker(
    question: str,
    options: list[str],
    workflows: list[dict[str, Any]] | None = None,
) -> bool:
    """True when this ask_user is a workflow menu, not a yes/no generate confirm."""
    rows = workflows if workflows is not None else _enabled_workflows()
    hits = sum(1 for opt in options if match_workflow(str(opt), rows))
    if hits >= 2:
        return True
    if hits >= 1 and _WHICH_WORKFLOW_RE.search(question or ""):
        return True
    return False


def recent_user_texts(session_id: int, limit: int = 4) -> list[str]:
    """Visible user lines, oldest first, each truncated. Steering excluded."""
    texts: list[str] = []
    for ev in session_log.read_events(session_id):
        if ev.type != session_log.USER_MESSAGE:
            continue
        data = ev.data if isinstance(ev.data, dict) else {}
        text = session_log.text_of_content(data.get("content") or "").strip()
        if text:
            texts.append(text[:300])
    return texts[-limit:]


def _cue_allowed(text: str, pattern: re.Pattern[str]) -> bool:
    """True when `pattern` hits a clause the user did not negate."""
    if _SCRIPT_REFUSAL_RE.search(text or ""):
        return False
    for clause in re.split(r"[,.;!?]|\bbut\b|\bhowever\b", text or "", flags=re.IGNORECASE):
        for match in pattern.finditer(clause):
            before = clause[max(0, match.start() - 60) : match.start()]
            after = clause[match.end() : match.end() + 40]
            if not _cue_is_negated(before, after):
                return True
    return False


def message_asks_for_script(text: str) -> bool:
    """True when this message asks for a script, scenes, shots, or a film."""
    return _cue_allowed(text or "", _SCRIPT_CUE_RE)


def user_asked_for_script(session_id: int) -> bool:
    return any(message_asks_for_script(text) for text in recent_user_texts(session_id, limit=8))


def prior_render_still_stands(session_id: int) -> bool:
    """True when an earlier yes-to-generate still applies.

    The render guard used to read only the latest line. After "Yes, generate
    all 4 now", a complaint ("your reasoning sucks") made run_workflow look
    unapproved. A new task ("add a character") or a cancel does not keep it.
    """
    texts = recent_user_texts(session_id, limit=8)
    if len(texts) < 2:
        return False
    latest = texts[-1]
    if _REVOKE_RENDER_RE.search(latest):
        return False
    if _NEW_TASK_RE.search(latest) and not is_render_request(latest):
        return False
    return any(
        (is_confirmation(text) and _GENERATE_RE.search(text)) or is_render_request(text)
        for text in texts[:-1]
    )


def user_already_approved_generation(session_id: int) -> bool:
    """True when an earlier user line already said yes to generating images."""
    for text in recent_user_texts(session_id, limit=8):
        if is_confirmation(text) and _GENERATE_RE.search(text):
            return True
    return False


def latest_message_names_other_work(session_id: int) -> bool:
    """True when the newest line asks for story, script, or video work."""
    texts = recent_user_texts(session_id, limit=1)
    if not texts:
        return False
    return _cue_allowed(texts[-1], _OTHER_WORK_RE)


def task_is_script_work(role: str, goal: str) -> bool:
    if role == "script":
        return True
    return bool(_SCRIPT_GOAL_RE.search(goal or ""))


def message_confirms_replacement(text: str) -> bool:
    """True when the user asked to replace or regenerate story/script content.

    "Yes, generate the images" confirms a render. It does not confirm
    generate_story.
    """
    return _cue_allowed(text or "", _REPLACE_CUE_RE)


def user_asked_to_rewrite_story(session_id: int) -> bool:
    return any(
        message_confirms_replacement(text) for text in recent_user_texts(session_id, limit=8)
    )


def task_rewrites_story(role: str, goal: str) -> bool:
    """True when this task would call generate_story or rewrite the storyline."""
    text = goal or ""
    if re.search(r"generate_story", text, re.IGNORECASE):
        return True
    return role == "story" and bool(_REPLACE_CUE_RE.search(text))


def conversation_for_agent(session_id: int) -> str:
    """Recent user lines for a sub-agent that otherwise starts with no memory."""
    lines = recent_user_texts(session_id, limit=8)
    if not lines:
        return ""
    body = "\n".join(f"- {line}" for line in lines)
    return (
        "CONVERSATION SO FAR (the user's own words, oldest first). "
        "These decisions stand. Do not ask them to repeat one. "
        "Do not regenerate the story or the script unless one of these lines "
        "asks to change it:\n"
        f"{body}\n"
    )
