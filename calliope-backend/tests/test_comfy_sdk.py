"""Comfy SDK and Router transport. The SDK client is mocked; nothing talks to Comfy."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from calliope.comfyui.router_models import (
    build_flux_body,
    build_h3_body,
    h3_duration,
    result_urls,
    user_asked_h3_router,
)
from calliope.config import settings
from calliope.db import get_db
from calliope.queue.manager import queue_manager
from calliope.queue.worker import QueueWorker, _CancelledByUser


def _nodes(image: str) -> dict:
    return {
        "1": {
            "class_type": "LoadImage",
            "inputs": {"image": image},
            "_meta": {"title": "Ref (Input:image)"},
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {},
            "_meta": {"title": "Result (Output:image)"},
        },
    }


class _Output:
    def __init__(self) -> None:
        self.node_id = "9"
        self.type = "image"
        self.name = "out.png"

    async def to_file(self, dest: Path) -> None:
        Path(dest).write_bytes(b"png-bytes")


class _Workflow:
    def __init__(self) -> None:
        self.inputs: list[tuple[str, str, object]] = []

    def set_input(self, node_id: str, field: str, value: object) -> None:
        self.inputs.append((node_id, field, value))


class _SdkJob:
    def __init__(self) -> None:
        self.id = "sdk-1"
        self.status = "running"
        self.error = None
        self.outputs: list[_Output] = []
        self.cancelled = False

    async def refresh(self) -> None:
        self.status = "succeeded"
        self.outputs = [_Output()]

    async def cancel(self) -> None:
        self.cancelled = True
        self.status = "canceled"


class _RouterHandle:
    def __init__(self, result: object) -> None:
        self.result = result
        self.cancelled = False

    async def status(self) -> object:
        return type("Update", (), {"status": "COMPLETED"})()

    async def get(self) -> object:
        return self.result

    async def cancel(self) -> None:
        self.cancelled = True


class _Sdk:
    def __init__(self) -> None:
        self.job = _SdkJob()
        self.workflow = _Workflow()
        self.assets_paths: list[str] = []
        self.submitted = False
        self.router_calls: list[tuple[str, dict]] = []
        self.handle = _RouterHandle(
            {"task": {"content": {"url": "https://cdn.example/clip.mp4"}}}
        )
        self.workflows = self
        self.assets = self
        self.models = self

    def from_json(self, _graph: dict) -> _Workflow:
        return self.workflow

    def from_file(self, path: str) -> dict:
        self.assets_paths.append(path)
        return {"path": path}

    async def submit(self, _wf: _Workflow) -> _SdkJob:
        self.submitted = True
        return self.job

    async def models_submit(self, model: str, body: dict) -> _RouterHandle:
        self.router_calls.append((model, body))
        return self.handle


@pytest.fixture(autouse=True)
def _pause_live_worker():
    queue_manager.paused = True
    yield
    queue_manager.paused = False


def _project(client) -> int:
    return client.post("/api/projects", json={"title": "SDK"}).json()["id"]


def _patch_image(monkeypatch, image: Path) -> _Sdk:
    sdk = _Sdk()

    class _Ctx:
        async def __aenter__(self):
            return sdk

        async def __aexit__(self, *_exc):
            return None

    def _open():
        return _Ctx()

    async def _proxy():
        return None

    def load(_self, _workflow_id):
        return {"nodes": _nodes(str(image)), "strict_mode": 0, "schema": []}

    monkeypatch.setattr("calliope.queue.worker.open_async_client", _open)
    monkeypatch.setattr("calliope.queue.worker.proxy_reachable", _proxy)
    monkeypatch.setattr(QueueWorker, "_load_workflow", load)
    return sdk


def test_h3_and_flux_bodies(tmp_path: Path) -> None:
    assert h3_duration(2) == 4
    assert h3_duration(99) == 15
    assert h3_duration(None) == 5
    image = tmp_path / "ref.png"
    image.write_bytes(b"img")
    refs = []
    for name in ("ada.png", "dock.png", "prop.png"):
        file = tmp_path / name
        file.write_bytes(b"img")
        refs.append({"path": str(file), "name": file.stem})
    h3 = build_h3_body("a cat walks", 8.4, refs)
    assert h3["duration"] == 8
    assert h3["ratio"] == "adaptive"
    assert h3["resolution"] == "768P"
    roles = [item.get("role") for item in h3["content"] if item["type"] == "image_url"]
    assert roles == ["reference_image", "reference_image", "reference_image"]
    assert "first_frame" not in str(h3)
    text = h3["content"][0]["text"]
    assert "Image 1 is ada." in text
    assert "Image 2 is dock." in text
    assert "Image 3 is prop." in text
    bare = build_h3_body("only words", None, [])
    assert bare["ratio"] == "16:9"
    assert len(bare["content"]) == 1
    assert user_asked_h3_router("Please use Comfy Router MiniMax H3")
    assert not user_asked_h3_router("render this with the local workflow")
    flux = build_flux_body("a portrait", str(image))
    assert flux["prompt"] == "a portrait"
    assert flux["images"]
    assert result_urls({"task": {"content": {"url": "https://cdn.example/a.mp4"}}}) == [
        "https://cdn.example/a.mp4"
    ]
    assert result_urls({"result": {"sample": "https://cdn.example/b.png"}}) == [
        "https://cdn.example/b.png"
    ]


def test_dry_run_does_not_construct_a_client(client, monkeypatch) -> None:
    opened = {"n": 0}

    def _open():
        opened["n"] += 1
        raise AssertionError("dry-run constructed a client")

    monkeypatch.setattr("calliope.queue.worker.open_async_client", _open)
    pid = _project(client)
    row = queue_manager.enqueue(
        project_id=pid,
        kind="image",
        payload={"router_model": "bfl/flux-3-image", "prompt": "x"},
    )
    paths = asyncio.run(QueueWorker()._run_job(row))
    assert opened["n"] == 0
    assert Path(paths[0]).is_file()
    assert paths[0].endswith(".png")


def test_workflow_job_submits_and_writes_output(client, monkeypatch, tmp_path: Path) -> None:
    image = tmp_path / "ref.png"
    image.write_bytes(b"img")
    sdk = _patch_image(monkeypatch, image)
    prev = settings.dry_run
    settings.dry_run = False
    try:
        pid = _project(client)
        row = queue_manager.enqueue(project_id=pid, kind="image", payload={})
        paths = asyncio.run(QueueWorker()._run_job(row))
    finally:
        settings.dry_run = prev
    assert sdk.submitted
    assert sdk.workflow.inputs
    assert sdk.workflow.inputs[0][0] == "1"
    assert Path(paths[0]).read_bytes() == b"png-bytes"
    assert str(settings.assets_dir) in paths[0]


def test_cancel_calls_sdk_cancel(client, monkeypatch, tmp_path: Path) -> None:
    image = tmp_path / "ref.png"
    image.write_bytes(b"img")
    sdk = _patch_image(monkeypatch, image)
    prev = settings.dry_run
    settings.dry_run = False
    try:
        pid = _project(client)
        row = queue_manager.enqueue(project_id=pid, kind="image", payload={})
        conn = get_db(settings.db_path)
        try:
            conn.execute("UPDATE jobs SET status = 'cancelled' WHERE id = ?", (row["id"],))
            conn.commit()
        finally:
            conn.close()
        with pytest.raises(_CancelledByUser):
            asyncio.run(QueueWorker()._run_job(row))
    finally:
        settings.dry_run = prev
    assert sdk.job.cancelled


def test_router_without_key_fails_closed(client, monkeypatch) -> None:
    opened = {"n": 0}

    def _open():
        opened["n"] += 1
        raise AssertionError("router opened a client without a key")

    monkeypatch.setattr("calliope.queue.worker.open_async_client", _open)
    prev_dry, prev_key = settings.dry_run, settings.comfy_api_key
    prev_enabled = settings.comfy_router_enabled
    settings.dry_run = False
    settings.comfy_router_enabled = True
    settings.comfy_api_key = None
    try:
        row = {
            "id": 1,
            "project_id": 1,
            "kind": "video",
            "payload_json": json.dumps(
                {"router_model": "minimax/minimax-h3", "prompt": "a cat"}
            ),
        }
        with pytest.raises(RuntimeError, match="API key"):
            asyncio.run(QueueWorker()._run_job(row))
    finally:
        settings.dry_run = prev_dry
        settings.comfy_api_key = prev_key
        settings.comfy_router_enabled = prev_enabled
    assert opened["n"] == 0


def test_router_switch_off_fails_closed_even_with_a_key(client, monkeypatch) -> None:
    opened = {"n": 0}

    def _open():
        opened["n"] += 1
        raise AssertionError("router opened a client while switched off")

    monkeypatch.setattr("calliope.queue.worker.open_async_client", _open)
    prev_dry, prev_key = settings.dry_run, settings.comfy_api_key
    prev_enabled = settings.comfy_router_enabled
    settings.dry_run = False
    settings.comfy_router_enabled = False
    settings.comfy_api_key = "saved-key"
    try:
        row = {
            "id": 1,
            "project_id": 1,
            "kind": "video",
            "payload_json": json.dumps(
                {"router_model": "minimax/minimax-h3", "prompt": "a cat"}
            ),
        }
        with pytest.raises(RuntimeError, match="turned off"):
            asyncio.run(QueueWorker()._run_job(row))
    finally:
        settings.dry_run = prev_dry
        settings.comfy_api_key = prev_key
        settings.comfy_router_enabled = prev_enabled
    assert opened["n"] == 0


def test_router_success_downloads_into_assets(client, monkeypatch) -> None:
    sdk = _Sdk()

    class _Ctx:
        async def __aenter__(self):
            return sdk

        async def __aexit__(self, *_exc):
            return None

    def _open():
        return _Ctx()

    async def _download(url: str, dest: Path) -> None:
        assert url == "https://cdn.example/clip.mp4"
        dest.write_bytes(b"mp4-bytes")

    sdk.models.submit = sdk.models_submit  # type: ignore[method-assign]
    monkeypatch.setattr("calliope.queue.worker.open_async_client", _open)
    monkeypatch.setattr("calliope.queue.worker.download_url", _download)
    prev_dry, prev_key = settings.dry_run, settings.comfy_api_key
    prev_enabled = settings.comfy_router_enabled
    settings.dry_run = False
    settings.comfy_router_enabled = True
    settings.comfy_api_key = "test-key"
    try:
        pid = _project(client)
        row = queue_manager.enqueue(
            project_id=pid,
            kind="video",
            payload={
                "router_model": "minimax/minimax-h3",
                "prompt": "a cat walks in",
                "duration_sec": 6,
            },
        )
        paths = asyncio.run(QueueWorker()._run_job(row))
    finally:
        settings.dry_run = prev_dry
        settings.comfy_api_key = prev_key
        settings.comfy_router_enabled = prev_enabled
    assert sdk.router_calls[0][0] == "minimax/minimax-h3"
    assert Path(paths[0]).read_bytes() == b"mp4-bytes"
    assert str(settings.assets_dir) in paths[0]


def test_settings_masks_comfy_api_key(client) -> None:
    prev_key, prev_url = settings.comfy_api_key, settings.comfy_api_base_url
    prev_enabled = settings.comfy_router_enabled
    try:
        saved = client.post(
            "/api/settings",
            json={
                "comfy_api_key": "super-secret",
                "comfy_api_base_url": "http://127.0.0.1:9191",
                "comfy_router_enabled": True,
            },
        )
        assert saved.status_code == 200
        body = saved.json()
        assert body["comfy_api_key"] is True
        assert body["comfy_api_base_url"] == "http://127.0.0.1:9191"
        assert body["comfy_router_enabled"] is True
        assert "super-secret" not in saved.text
        switched = client.post("/api/settings", json={"comfy_router_enabled": False})
        assert switched.json()["comfy_router_enabled"] is False
        cleared = client.post("/api/settings", json={"comfy_api_key": ""})
        assert cleared.json()["comfy_api_key"] is False
        blank = client.post("/api/settings", json={"comfy_api_base_url": "  "})
        assert blank.json()["comfy_api_base_url"] == "http://127.0.0.1:8189"
    finally:
        settings.comfy_api_key = prev_key
        settings.comfy_api_base_url = prev_url
        settings.comfy_router_enabled = prev_enabled
