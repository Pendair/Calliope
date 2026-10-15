"""H3 motion-context chains: a clip continues the clip before it.

A workflow with ComfyUI-H3-Motion-Context's latent pair (Load Latent → Motion
Context → … → Save Latent) pins the previous clip's saved AV latent at the head
of the new clip, so motion and sound carry across the join. Calliope owns every
file in the chain:

- After a render, the Save node's latent is downloaded over ``/view`` into
  ``assets/<project>/latent/`` and recorded on the clip's ``video_settings``.
- Before a render that continues, the previous clip's latent is uploaded over
  ``/upload/image`` into ComfyUI's input folder and the Load node reads it from
  there (``folder: "input"``). ComfyUI's own output folder is never relied on.

Resolution happens when the job RUNS, not when it is queued: Generate All
queues clip N before clip N−1 has rendered.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from calliope.comfyui.registry import LATENT_INPUT_CLASSES, LATENT_OUTPUT_CLASSES

LATENT_DIR = "latent"

# video_settings keys written by the worker, never by the clip form. The form
# autosave PATCHes the whole video_settings dict from a possibly stale copy;
# update_clip carries these over so a form edit cannot erase a chain.
SERVER_OWNED_KEYS = (
    "latent_path",
    "latent_job_id",
    "continued_from_clip_id",
    "continued_from_job_id",
    "renders",
)


class ChainDeferred(Exception):
    """The previous clip is still rendering; run this job after it."""

    def __init__(self, blocking_job_id: int, message: str) -> None:
        super().__init__(message)
        self.blocking_job_id = blocking_job_id


class ChainError(RuntimeError):
    """The clip continues one that cannot be continued."""


def latent_nodes(nodes: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(Load Latent node ids, Save Latent node ids) in a workflow, by class."""
    load: list[str] = []
    save: list[str] = []
    for nid, node in nodes.items():
        if not isinstance(node, dict):
            continue
        cls = node.get("class_type")
        if cls in LATENT_INPUT_CLASSES:
            load.append(str(nid))
        elif cls in LATENT_OUTPUT_CLASSES:
            save.append(str(nid))
    return load, save


def ordered_clip_ids(conn: sqlite3.Connection, project_id: int) -> list[int]:
    """Every clip of a project in playback order.

    Same order as the Video stage's filmstrip and Generate All (scenes by
    order_index, then each scene's clips as the scenes API lists them) and as
    the film export.
    """
    rows = conn.execute(
        """
        SELECT c.id FROM clips c JOIN scenes s ON s.id = c.scene_id
        WHERE c.project_id = ?
        ORDER BY s.order_index, s.id, c.order_index, c.id
        """,
        (project_id,),
    ).fetchall()
    return [int(r["id"]) for r in rows]


def previous_clip_id(conn: sqlite3.Connection, project_id: int, clip_id: int) -> int | None:
    order = ordered_clip_ids(conn, project_id)
    try:
        pos = order.index(int(clip_id))
    except ValueError:
        return None
    return order[pos - 1] if pos > 0 else None


def clip_position(conn: sqlite3.Connection, project_id: int, clip_id: int) -> int:
    """1-based playback position, for messages ("clip 3")."""
    order = ordered_clip_ids(conn, project_id)
    return order.index(int(clip_id)) + 1 if int(clip_id) in order else 0


def read_settings(conn: sqlite3.Connection, clip_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT video_settings_json FROM clips WHERE id = ?", (clip_id,)).fetchone()
    if not row:
        return {}
    try:
        data = json.loads(row["video_settings_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_settings(conn: sqlite3.Connection, clip_id: int, data: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE clips SET video_settings_json = ? WHERE id = ?",
        (json.dumps(data, ensure_ascii=False), clip_id),
    )


def is_chain_start(settings: dict[str, Any], prev_id: int | None) -> bool:
    """A clip with nothing before it always starts a chain; otherwise it
    continues unless the user ticked "Start a new chain here"."""
    if prev_id is None:
        return True
    return bool(settings.get("chain_start", False))


def keep_server_owned(incoming: dict[str, Any], existing: dict[str, Any]) -> dict[str, Any]:
    """Merge a form PATCH of video_settings with the worker-owned keys.

    ``chain_start`` is user-owned but missing from older form payloads, so a
    payload without it keeps the stored value instead of resetting it.
    """
    out = dict(incoming)
    for key in SERVER_OWNED_KEYS:
        if key in existing:
            out[key] = existing[key]
        else:
            out.pop(key, None)
    if "chain_start" not in out and "chain_start" in existing:
        out["chain_start"] = existing["chain_start"]
    return out


def latent_dest(assets_dir: Path, project_id: int, clip_id: int, job_id: int) -> Path:
    """One file per render, so render-history versioning can switch back to it."""
    return Path(assets_dir) / str(project_id) / LATENT_DIR / f"c{clip_id}_job{job_id}.safetensors"


def upload_name(project_id: int, prev_clip_id: int) -> str:
    """Flat name in ComfyUI's input folder; a re-upload overwrites it."""
    return f"calliope_p{project_id}_c{prev_clip_id}.safetensors"


def save_prefix(project_id: int, clip_id: int | None, job_id: int) -> str:
    # Fixed slot (clip_index 1) so a re-roll overwrites instead of stacking files
    # in ComfyUI's output folder. Clip-less jobs (Playground) get a per-job name.
    if clip_id is None:
        return f"calliope/p{project_id}_job{job_id}"
    return f"calliope/p{project_id}_c{clip_id}"


def resolve(
    conn: sqlite3.Connection,
    job: dict[str, Any],
    nodes: dict[str, Any],
) -> dict[str, Any] | None:
    """Plan this job's chain patch, or None when the workflow has no latent pair.

    Returns ``{"values": {node_id: {widget: value}}, "upload": Path | None,
    "upload_name": str | None, "prev_clip_id", "prev_job_id", "chain_start"}``.
    Raises ChainDeferred while the previous clip is still rendering, and
    ChainError when it has no saved latent.
    """
    load_ids, save_ids = latent_nodes(nodes)
    if not load_ids and not save_ids:
        return None
    project_id = int(job["project_id"])
    clip_id = job.get("clip_id")
    values: dict[str, dict[str, Any]] = {}
    for nid in save_ids:
        values[nid] = {
            "filename_prefix": save_prefix(project_id, clip_id, int(job["id"])),
            "clip_index": 1,
        }

    plan: dict[str, Any] = {
        "values": values,
        "upload": None,
        "upload_name": None,
        "prev_clip_id": None,
        "prev_job_id": None,
        "chain_start": True,
    }
    prev_id = previous_clip_id(conn, project_id, clip_id) if clip_id is not None else None
    settings = read_settings(conn, clip_id) if clip_id is not None else {}
    if is_chain_start(settings, prev_id) or not load_ids:
        for nid in load_ids:
            values[nid] = {"clip_index": 0, "folder": "input"}
        return plan

    blocking = conn.execute(
        """
        SELECT id FROM jobs
        WHERE clip_id = ? AND kind = 'video' AND status IN ('pending', 'running') AND id != ?
        ORDER BY id DESC LIMIT 1
        """,
        (prev_id, job["id"]),
    ).fetchone()
    here = clip_position(conn, project_id, clip_id)
    there = clip_position(conn, project_id, prev_id)
    if blocking:
        raise ChainDeferred(
            int(blocking["id"]),
            f"Clip {here} waits for clip {there} to finish rendering.",
        )
    prev = read_settings(conn, prev_id)
    latent = prev.get("latent_path")
    if not latent or not Path(latent).is_file():
        raise ChainError(
            f"Clip {here} continues clip {there}, but clip {there} has no saved latent. "
            f"Render clip {there} with a motion-context workflow first, or tick "
            '"Start a new chain here" on clip '
            f"{here}."
        )
    name = upload_name(project_id, prev_id)
    for nid in load_ids:
        values[nid] = {"latent_path": name, "clip_index": 1, "folder": "input"}
    plan.update(
        upload=Path(latent),
        upload_name=name,
        prev_clip_id=prev_id,
        prev_job_id=prev.get("latent_job_id"),
        chain_start=False,
    )
    return plan


def record_render(
    conn: sqlite3.Connection,
    clip_id: int,
    job_id: int,
    latent_path: str | None,
    plan: dict[str, Any] | None,
) -> None:
    """Store a finished render's chain facts on the clip (active + per-job)."""
    data = read_settings(conn, clip_id)
    entry = {
        "latent_path": latent_path,
        "continued_from_clip_id": plan.get("prev_clip_id") if plan else None,
        "continued_from_job_id": plan.get("prev_job_id") if plan else None,
    }
    renders = data.get("renders") if isinstance(data.get("renders"), dict) else {}
    renders[str(job_id)] = entry
    data["renders"] = renders
    data["latent_job_id"] = job_id if latent_path else None
    data.update(entry)
    write_settings(conn, clip_id, data)


def activate_render(conn: sqlite3.Connection, clip_id: int, clip_path: str) -> None:
    """Render-history versioning picked an older output: follow its chain facts.

    A render with no recorded entry (pre-chain, or a non-motion workflow) has
    no latent, so the clip can no longer be continued until it is re-rendered.
    """
    data = read_settings(conn, clip_id)
    renders = data.get("renders") if isinstance(data.get("renders"), dict) else {}
    if not renders and "latent_path" not in data:
        return
    job_id = None
    for row in conn.execute(
        "SELECT id, output_paths_json FROM jobs WHERE clip_id = ? ORDER BY id DESC",
        (clip_id,),
    ).fetchall():
        try:
            outs = json.loads(row["output_paths_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        if clip_path in outs:
            job_id = int(row["id"])
            break
    entry = renders.get(str(job_id)) if job_id is not None else None
    if entry:
        data.update(entry)
        data["latent_job_id"] = job_id if entry.get("latent_path") else None
    else:
        for key in (
            "latent_path",
            "latent_job_id",
            "continued_from_clip_id",
            "continued_from_job_id",
        ):
            data[key] = None
    write_settings(conn, clip_id, data)
