"""H3 motion-context chains: latent slots, run-time chain resolution, hard cuts."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest

from calliope.comfyui.client import ComfyUIClient, select_output_files
from calliope.comfyui.parser import parse_dynamic_inputs, parse_dynamic_outputs
from calliope.comfyui.patcher import patch_workflow
from calliope.comfyui.registry import (
    class_to_input_kind,
    class_to_output_kind,
    class_to_patch_field,
)
from calliope.config import settings
from calliope.db import get_db
from calliope.export.runner import build_ffmpeg_cmd, join_cuts, output_duration
from calliope.queue import chain
from calliope.queue.manager import queue_manager
from calliope.queue.worker import QueueWorker, _apply_strict_mode

LOAD = "MiniMaxH3MotionContextLoadLatent"
SAVE = "MiniMaxH3MotionContextSaveLatent"


def _workflow() -> dict:
    return {
        "429": {
            "class_type": LOAD,
            "inputs": {"latent_path": "h3_context", "clip_index": 1},
            "_meta": {"title": "(input:latent) Latent"},
        },
        "365": {
            "class_type": "MiniMaxH3MotionContext",
            "inputs": {"context_latent": ["429", 0], "context_length": "22"},
            "_meta": {"title": "Motion Context"},
        },
        "404": {
            "class_type": SAVE,
            "inputs": {"latent": ["300", 0], "filename_prefix": "h3_context/clip", "clip_index": 1},
            "_meta": {"title": "(output:latent) Latent"},
        },
        "500": {
            "class_type": "SaveVideo",
            "inputs": {"video": ["400", 0]},
            "_meta": {"title": "(Output:video) Clip"},
        },
    }


# --- B1: detection --------------------------------------------------------


def test_latent_classes_resolve_to_latent_not_image():
    assert class_to_input_kind(LOAD) == "latent"
    assert class_to_patch_field(LOAD) == "latent_path"
    assert class_to_output_kind(SAVE) == "latent"
    # the substring fallback still calls an unknown loader an image
    assert class_to_input_kind("SomeOtherLoader") == "image"


def test_parser_tags_latent_role_and_kind():
    inputs = parse_dynamic_inputs(_workflow())
    (latent,) = [i for i in inputs if i["nodeId"] == "429"]
    assert latent["role"] == "latent" and latent["kind"] == "latent"
    assert latent["defaultValue"] is None  # the file's own path is never a form default
    outputs = {o["nodeId"]: o for o in parse_dynamic_outputs(_workflow())}
    assert outputs["404"]["kind"] == "latent" and outputs["404"]["role"] == "latent"
    assert outputs["500"]["kind"] == "video"


def test_shipped_continuity_example_has_one_latent_pair():
    path = (
        Path(__file__).resolve().parents[2]
        / "example_ComfyUI_workflows"
        / "MiniMaxH3-Singularity-Continuity-5Ref1Latent-API.json"
    )
    wf = json.loads(path.read_text(encoding="utf-8"))
    assert chain.latent_nodes(wf) == (["429"], ["404"])
    assert wf["429"]["inputs"]["folder"] == "input"
    # H3's native 0.98 MP stays a string: PrimitiveFloat's 0.1 step rounds it
    # to 1.0 when the workflow is opened in the ComfyUI editor.
    assert wf["422"]["class_type"] == "PrimitiveString"
    assert patch_workflow(wf, {"422": ".98"})["422"]["inputs"]["value"] == ".98"
    roles = {i["role"] for i in parse_dynamic_inputs(wf)}
    assert {"prompt", "duration", "image", "latent"} <= roles
    kinds = {o["kind"] for o in parse_dynamic_outputs(wf)}
    assert kinds == {"latent", "video"}


def test_example_workflows_have_no_control_characters():
    # A mangled "\v" once turned a video prefix into a vertical tab, which
    # Windows rejects as a file name only after the whole render has run.
    folder = Path(__file__).resolve().parents[2] / "example_ComfyUI_workflows"
    for path in sorted(folder.glob("*.json")):
        wf = json.loads(path.read_text(encoding="utf-8"))
        for nid, node in wf.items():
            for name, value in node.get("inputs", {}).items():
                if isinstance(value, str):
                    bad = [c for c in value if ord(c) < 32 and c not in "\n\t"]
                    assert not bad, f"{path.name} node {nid} {name}: {value!r}"


def test_strict_mode_keeps_latent_slot_even_with_a_stale_image_schema():
    wf = _workflow()
    # A workflow imported before the latent kind existed stored kind "image".
    schema = [{"nodeId": "429", "role": None, "kind": "image"}]
    out = _apply_strict_mode(copy.deepcopy(wf), wf, schema, {})
    assert "429" in out
    assert out["365"]["inputs"]["context_latent"] == ["429", 0]


# --- B2: multi-field patching ----------------------------------------------


def test_patch_dict_value_sets_several_widgets():
    wf = _workflow()
    out = patch_workflow(
        wf, {"429": {"latent_path": "x.safetensors", "clip_index": 1, "folder": "input"}}
    )
    assert out["429"]["inputs"] == {
        "latent_path": "x.safetensors",
        "clip_index": 1,
        "folder": "input",
    }
    assert wf["429"]["inputs"]["latent_path"] == "h3_context"  # base untouched


def test_patch_plain_value_unchanged():
    wf = {"7": {"class_type": "PrimitiveStringMultiline", "inputs": {"value": "a"}}}
    assert patch_workflow(wf, {"7": "b"})["7"]["inputs"] == {"value": "b"}
    # a plain value on the latent node lands on latent_path (single-field path)
    assert patch_workflow(_workflow(), {"429": "y"})["429"]["inputs"]["latent_path"] == "y"


# --- B3: chain resolution ---------------------------------------------------


def _clip_ids(conn, pid: int) -> list[int]:
    return chain.ordered_clip_ids(conn, pid)


def _project_with_three_clips(client) -> tuple[int, list[int]]:
    pid = client.post("/api/projects", json={"title": "Chain"}).json()["id"]
    s1 = client.post(f"/api/projects/{pid}/scenes", json={"order_index": 1, "heading": "A"}).json()
    s2 = client.post(f"/api/projects/{pid}/scenes", json={"order_index": 2, "heading": "B"}).json()
    client.post(f"/api/projects/{pid}/scenes/{s1['id']}/clips", json={"description": "second shot"})
    conn = get_db(settings.db_path)
    try:
        ids = _clip_ids(conn, pid)
        # scene 1: default clip + added clip, scene 2: default clip
        scene_of = {
            r["id"]: r["scene_id"]
            for r in conn.execute("SELECT id, scene_id FROM clips WHERE project_id = ?", (pid,))
        }
    finally:
        conn.close()
    assert len(ids) == 3
    assert [scene_of[i] for i in ids] == [s1["id"], s1["id"], s2["id"]]
    return pid, ids


def _job(pid: int, clip_id: int, job_id: int = 9999) -> dict:
    return {"id": job_id, "project_id": pid, "clip_id": clip_id, "kind": "video"}


def _set_latent(clip_id: int, path: Path, job_id: int = 41) -> None:
    conn = get_db(settings.db_path)
    try:
        chain.record_render(conn, clip_id, job_id, str(path), None)
        conn.commit()
    finally:
        conn.close()


def _resolve(pid: int, clip_id: int, job_id: int = 9999):
    conn = get_db(settings.db_path)
    try:
        return chain.resolve(conn, _job(pid, clip_id, job_id), _workflow())
    finally:
        conn.close()


def test_first_clip_starts_a_chain(client):
    pid, ids = _project_with_three_clips(client)
    plan = _resolve(pid, ids[0])
    assert plan["chain_start"] is True and plan["upload"] is None
    assert plan["values"]["429"] == {"clip_index": 0, "folder": "input"}
    assert plan["values"]["404"] == {
        "filename_prefix": f"calliope/p{pid}_c{ids[0]}",
        "clip_index": 1,
    }


def test_chain_start_flag_starts_a_new_chain(client):
    pid, ids = _project_with_three_clips(client)
    client.patch(
        f"/api/projects/{pid}/clips/{ids[1]}", json={"video_settings": {"chain_start": True}}
    )
    plan = _resolve(pid, ids[1])
    assert plan["chain_start"] is True
    assert plan["values"]["429"]["clip_index"] == 0


def test_previous_with_latent_uploads_and_loads_index_1(client, tmp_path):
    pid, ids = _project_with_three_clips(client)
    latent = tmp_path / "prev.safetensors"
    latent.write_bytes(b"x")
    _set_latent(ids[1], latent, job_id=41)
    plan = _resolve(pid, ids[2])  # crosses the scene boundary: clip 3 continues clip 2
    name = f"calliope_p{pid}_c{ids[1]}.safetensors"
    assert plan["chain_start"] is False
    assert plan["upload"] == latent and plan["upload_name"] == name
    assert plan["values"]["429"] == {"latent_path": name, "clip_index": 1, "folder": "input"}
    assert plan["prev_clip_id"] == ids[1] and plan["prev_job_id"] == 41


def test_previous_without_latent_fails_clearly(client):
    pid, ids = _project_with_three_clips(client)
    with pytest.raises(chain.ChainError) as exc:
        _resolve(pid, ids[1])
    msg = str(exc.value)
    assert "Clip 2 continues clip 1, but clip 1 has no saved latent" in msg
    assert "Start a new chain here" in msg


def test_previous_latent_file_missing_on_disk_fails(client, tmp_path):
    pid, ids = _project_with_three_clips(client)
    _set_latent(ids[0], tmp_path / "gone.safetensors")
    with pytest.raises(chain.ChainError):
        _resolve(pid, ids[1])


def test_previous_still_rendering_defers(client):
    pid, ids = _project_with_three_clips(client)
    blocking = queue_manager.enqueue(project_id=pid, kind="video", clip_id=ids[0])
    with pytest.raises(chain.ChainDeferred) as exc:
        _resolve(pid, ids[1])
    assert exc.value.blocking_job_id == blocking["id"]
    assert "waits for clip 1" in str(exc.value)


def test_workflow_without_latent_pair_has_no_plan(client):
    pid, ids = _project_with_three_clips(client)
    conn = get_db(settings.db_path)
    try:
        plan = chain.resolve(
            conn, _job(pid, ids[1]), {"1": {"class_type": "SaveVideo", "inputs": {}}}
        )
    finally:
        conn.close()
    assert plan is None


def test_defer_after_puts_the_job_behind_the_blocking_job(client):
    pid, ids = _project_with_three_clips(client)
    later = queue_manager.enqueue(project_id=pid, kind="video", clip_id=ids[1])
    earlier_prev = queue_manager.enqueue(project_id=pid, kind="video", clip_id=ids[0])
    claimed = queue_manager.claim_next()
    assert claimed["id"] == later["id"]
    queue_manager.defer_after(later["id"], earlier_prev["id"])
    assert queue_manager.get_job(later["id"])["status"] == "pending"
    assert queue_manager.claim_next()["id"] == earlier_prev["id"]
    assert queue_manager.claim_next()["id"] == later["id"]


# --- B4: collecting the latent ----------------------------------------------


def _history() -> dict:
    return {
        "outputs": {
            "500": {"videos": [{"filename": "clip.mp4", "subfolder": "", "type": "output"}]},
            "404": {
                "latents": [
                    {
                        "filename": "p1_c2_00001.safetensors",
                        "subfolder": "calliope",
                        "type": "output",
                        "id": "comfy-asset-id",
                    }
                ]
            },
        }
    }


def test_extract_outputs_collects_latents_tagged_by_key():
    files = ComfyUIClient.extract_outputs(None, _history())  # type: ignore[arg-type]
    latents = [f for f in files if f["key"] == "latents"]
    assert latents == [
        {
            "filename": "p1_c2_00001.safetensors",
            "subfolder": "calliope",
            "type": "output",
            "node_id": "404",
            "key": "latents",
        }
    ]


def test_latents_never_returned_as_the_video():
    files = ComfyUIClient.extract_outputs(None, _history())  # type: ignore[arg-type]
    tagged = select_output_files(files, _workflow(), "video")
    assert [f["filename"] for f in tagged] == ["clip.mp4"]
    untagged_nodes = copy.deepcopy(_workflow())
    untagged_nodes["500"]["_meta"]["title"] = "Clip"  # nothing tagged: fallback path
    fallback = select_output_files(files, untagged_nodes, "video")
    assert [f["filename"] for f in fallback] == ["clip.mp4"]


class _FakeClient:
    def __init__(self, supported: bool = True) -> None:
        self.supported = supported
        self.uploads: list[tuple[Path, str]] = []
        self.downloads: list[tuple[str, str, Path]] = []

    async def latent_input_supported(self) -> bool:
        return self.supported

    async def upload_latent(self, path: Path, name: str) -> str:
        self.uploads.append((path, name))
        return name

    async def download_image(self, filename, subfolder="", folder_type="output", dest=None):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"latent")
        self.downloads.append((filename, subfolder, dest))
        return dest


def test_apply_chain_uploads_and_patches(tmp_path):
    latent = tmp_path / "prev.safetensors"
    latent.write_bytes(b"x")
    plan = {
        "values": {
            "429": {
                "latent_path": "calliope_p1_c2.safetensors",
                "clip_index": 1,
                "folder": "input",
            },
            "404": {"filename_prefix": "calliope/p1_c3", "clip_index": 1},
        },
        "upload": latent,
        "upload_name": "calliope_p1_c2.safetensors",
    }
    fake = _FakeClient()
    out = asyncio.run(QueueWorker()._apply_chain(fake, plan, _workflow()))
    assert fake.uploads == [(latent, "calliope_p1_c2.safetensors")]
    assert out["429"]["inputs"]["folder"] == "input"
    assert out["429"]["inputs"]["clip_index"] == 1
    assert out["404"]["inputs"]["filename_prefix"] == "calliope/p1_c3"
    assert out["404"]["inputs"]["latent"] == ["300", 0]  # links survive


def test_apply_chain_refuses_a_pack_without_input_folder(tmp_path):
    latent = tmp_path / "prev.safetensors"
    latent.write_bytes(b"x")
    plan = {
        "values": {"429": {"latent_path": "n", "clip_index": 1}},
        "upload": latent,
        "upload_name": "n",
    }
    with pytest.raises(RuntimeError, match="folder"):
        asyncio.run(QueueWorker()._apply_chain(_FakeClient(supported=False), plan, _workflow()))


def test_collect_latent_records_the_render(client):
    pid, ids = _project_with_three_clips(client)
    job = {"id": 77, "project_id": pid, "clip_id": ids[1], "kind": "video"}
    plan = {"values": {"404": {"filename_prefix": "x"}}, "prev_clip_id": ids[0], "prev_job_id": 41}
    fake = _FakeClient()
    files = ComfyUIClient.extract_outputs(None, _history())  # type: ignore[arg-type]
    asyncio.run(QueueWorker()._collect_latent(fake, job, plan, files))
    dest = settings.assets_dir / str(pid) / "latent" / f"c{ids[1]}_job77.safetensors"
    assert fake.downloads == [("p1_c2_00001.safetensors", "calliope", dest)]
    conn = get_db(settings.db_path)
    try:
        vs = chain.read_settings(conn, ids[1])
    finally:
        conn.close()
    assert vs["latent_path"] == str(dest) and vs["latent_job_id"] == 77
    assert vs["continued_from_clip_id"] == ids[0] and vs["continued_from_job_id"] == 41
    assert vs["renders"]["77"]["latent_path"] == str(dest)


def test_form_autosave_cannot_erase_chain_state(client, tmp_path):
    pid, ids = _project_with_three_clips(client)
    client.patch(
        f"/api/projects/{pid}/clips/{ids[1]}", json={"video_settings": {"chain_start": True}}
    )
    _set_latent(ids[1], tmp_path / "l.safetensors", job_id=5)
    # the autosave payload: only form keys, no chain keys
    r = client.patch(
        f"/api/projects/{pid}/clips/{ids[1]}",
        json={"video_settings": {"input_values": {"1": "x"}, "latent_path": "forged"}},
    )
    vs = r.json()["video_settings"]
    assert vs["input_values"] == {"1": "x"}
    assert vs["latent_path"] == str(tmp_path / "l.safetensors")  # server-owned wins
    assert vs["latent_job_id"] == 5
    assert vs["chain_start"] is True  # kept when the payload omits it
    vs2 = client.patch(
        f"/api/projects/{pid}/clips/{ids[1]}", json={"video_settings": {"chain_start": False}}
    ).json()["video_settings"]
    assert vs2["chain_start"] is False


def test_applying_an_older_render_switches_its_latent(client, tmp_path):
    pid, ids = _project_with_three_clips(client)
    old = queue_manager.enqueue(project_id=pid, kind="video", clip_id=ids[1])
    new = queue_manager.enqueue(project_id=pid, kind="video", clip_id=ids[1])
    queue_manager.mark_done(old["id"], ["C:/r/old.mp4"])
    queue_manager.mark_done(new["id"], ["C:/r/new.mp4"])
    conn = get_db(settings.db_path)
    try:
        chain.record_render(
            conn,
            ids[1],
            old["id"],
            "C:/l/old.safetensors",
            {"prev_clip_id": ids[0], "prev_job_id": 3},
        )
        chain.record_render(
            conn,
            ids[1],
            new["id"],
            "C:/l/new.safetensors",
            {"prev_clip_id": ids[0], "prev_job_id": 4},
        )
        conn.commit()
    finally:
        conn.close()
    vs = client.patch(
        f"/api/projects/{pid}/clips/{ids[1]}", json={"clip_path": "C:/r/old.mp4"}
    ).json()["video_settings"]
    assert vs["latent_path"] == "C:/l/old.safetensors" and vs["latent_job_id"] == old["id"]
    assert vs["continued_from_job_id"] == 3
    # an output with no recorded chain entry leaves nothing to continue from
    vs = client.patch(
        f"/api/projects/{pid}/clips/{ids[1]}", json={"clip_path": "C:/r/elsewhere.mp4"}
    ).json()["video_settings"]
    assert vs["latent_path"] is None and vs["continued_from_clip_id"] is None


# --- B5: hard cuts on continued joins ----------------------------------------


def test_join_cuts_only_on_exact_continuation_pairs():
    def clip(cid: int, cont: int | None) -> dict:
        vs = {"continued_from_clip_id": cont} if cont is not None else {}
        return {"id": cid, "clip_path": f"{cid}.mp4", "video_settings_json": json.dumps(vs)}

    # 2 continues 1 (cut); 3 starts fresh (fade); 5 continues 4 but 4 was skipped (fade)
    clips = [clip(1, None), clip(2, 1), clip(3, None), clip(5, 4)]
    assert join_cuts(clips) == [True, False, False]


def test_filter_graph_hard_cuts_exactly_on_continued_joins():
    clips = [{"clip_path": f"{i}.mp4"} for i in range(4)]
    probes = [{"duration": d, "has_audio": True} for d in (3.0, 3.0, 4.0, 5.0)]
    cmd = build_ffmpeg_cmd(clips, probes, Path("out.mp4"), cuts=[True, False, True])
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "[v0][v1]concat=n=2:v=1:a=0[x1]" in graph
    assert "[a0][a1]concat=n=2:v=0:a=1[c1]" in graph
    # join 2 fades out of a 6.0 s stitched head: offset 6.0 - 0.5
    assert "[x1][v2]xfade=transition=fade:duration=0.5:offset=5.500[x2]" in graph
    assert "[c1][a2]acrossfade=d=0.5:c1=tri:c2=tri[c2]" in graph
    assert "[x2][v3]concat=n=2:v=1:a=0[x3]" in graph
    assert graph.count("xfade=") == 1 and graph.count("acrossfade=") == 1
    assert "[x3]format=yuv420p[vout]" in graph
    assert output_duration([3.0, 3.0, 4.0, 5.0], [True, False, True]) == pytest.approx(14.5)


def test_filter_graph_without_cuts_is_unchanged():
    clips = [{"clip_path": "a.mp4"}, {"clip_path": "b.mp4"}, {"clip_path": "c.mp4"}]
    probes = [{"duration": d, "has_audio": True} for d in (8.0, 6.0, 4.0)]
    default = build_ffmpeg_cmd(clips, probes, Path("o.mp4"))
    assert default == build_ffmpeg_cmd(clips, probes, Path("o.mp4"), cuts=[False, False])
    graph = default[default.index("-filter_complex") + 1]
    assert "offset=7.500[x1]" in graph and "offset=13.000[x2]" in graph
    assert "concat=" not in graph
