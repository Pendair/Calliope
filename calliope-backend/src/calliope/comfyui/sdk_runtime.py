"""Comfy API v2 client (comfy-sdk) for workflow jobs.

The SDK talks to comfy-api-proxy (default http://127.0.0.1:8189), which fronts
the ComfyUI named by settings.comfyui_base_url. Local media paths become SDK
assets. A bare filename already uploaded to ComfyUI — H3 motion-context
latents — is left as a string; the proxy forwards it on to /prompt.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import httpx

from calliope.comfyui.client import ComfyUIClient
from calliope.comfyui.registry import (
    AUDIO_CLASSES,
    IMAGE_CLASSES,
    VIDEO_CLASSES,
    VIDEO_FILE_CLASSES,
)
from calliope.config import settings

_ENV_LOCK = threading.Lock()
_OUTPUT_KEYS = {
    "image": "images",
    "video": "videos",
    "audio": "audio",
    "latent": "latents",
}


def api_base_url() -> str:
    return (settings.comfy_api_base_url or "http://127.0.0.1:8189").rstrip("/")


async def proxy_reachable() -> None:
    """Fail before submit when the Comfy API proxy is not up."""
    url = api_base_url()
    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            resp = await http.get(f"{url}/api/v2/health")
            if resp.status_code == 200:
                return
            detail = f"HTTP {resp.status_code}"
    except Exception as exc:
        detail = str(exc) or type(exc).__name__
    raise RuntimeError(
        f"Comfy API proxy unreachable at {url} ({detail}). "
        f"Start it with `comfy-api-proxy` so it fronts ComfyUI at "
        f"{settings.comfyui_base_url}."
    )


class SdkClient:
    """Async context manager around AsyncComfy.

    COMFY_BASE_URL is read at construction, not passed as an argument, so it
    is set from settings under a lock for the duration of that construction.
    """

    def __init__(self) -> None:
        self._client: Any = None

    async def __aenter__(self) -> Any:
        from comfy_sdk import AsyncComfy

        key = (settings.comfy_api_key or "").strip() or None
        with _ENV_LOCK:
            os.environ["COMFY_BASE_URL"] = api_base_url()
            self._client = AsyncComfy(api_key=key, client_info="calliope")
        return self._client

    async def __aexit__(self, *exc: object) -> None:
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()


def open_async_client() -> SdkClient:
    return SdkClient()


def bind_local_media(client: Any, workflow: dict[str, Any]) -> Any:
    """Validate media inputs and replace local files with SDK asset handles.

    Bare Comfy-side names (including latent filenames uploaded on port 8188)
    stay as strings. Returns the SDK workflow object wrapping ``workflow``.
    """
    wf = client.workflows.from_json(workflow)
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type", "")
        inputs = node.get("inputs") or {}
        if class_type in IMAGE_CLASSES:
            field = "image"
        elif class_type in AUDIO_CLASSES:
            field = next((k for k in ("audio", "audio:") if k in inputs), "audio")
        elif class_type in VIDEO_CLASSES:
            field = "file" if class_type in VIDEO_FILE_CLASSES else "video"
        else:
            continue
        media = inputs.get(field)
        if not isinstance(media, str):
            continue
        if not media.strip():
            if ComfyUIClient._references_node(workflow, str(node_id)):
                ComfyUIClient._reject_empty_media(str(node_id), class_type, field, node)
            continue
        if not ComfyUIClient._looks_like_local_path(media):
            continue
        ComfyUIClient._reject_bad_media_path(media, str(node_id), class_type, field)
        wf.set_input(str(node_id), field, client.assets.from_file(str(Path(media))))
    return wf


def output_records(outputs: list[Any]) -> list[dict[str, Any]]:
    """SDK outputs in the shape select_output_files already understands.

    Each record keeps the live handle under ``_output`` so the worker can
    download it without a second lookup.
    """
    records: list[dict[str, Any]] = []
    for out in outputs:
        kind = str(getattr(out, "type", "") or "")
        records.append(
            {
                "filename": str(getattr(out, "name", "") or "output"),
                "subfolder": "",
                "type": "output",
                "node_id": str(getattr(out, "node_id", "") or ""),
                "key": _OUTPUT_KEYS.get(kind, kind or "file"),
                "_output": out,
            }
        )
    return records
