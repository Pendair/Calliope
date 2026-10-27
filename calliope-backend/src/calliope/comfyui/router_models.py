"""Opt-in Comfy Router bodies for the two contest partner models.

Local workflow jobs never come through here. A job carries ``router_model``
only when the user picked it.
"""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import httpx

ROUTER_VIDEO_MODEL = "minimax/minimax-h3"
ROUTER_IMAGE_MODEL = "bfl/flux-3-image"
ROUTER_BY_KIND = {
    "video": ROUTER_VIDEO_MODEL,
    "image": ROUTER_IMAGE_MODEL,
}

_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def normalize_router_model(model: str | None, kind: str) -> str | None:
    """Return the canonical id, or None when the caller did not ask for Router."""
    text = (model or "").strip()
    if not text:
        return None
    expected = ROUTER_BY_KIND.get(kind)
    if text != expected:
        allowed = expected or "none"
        raise ValueError(
            f"router_model {text!r} does not match a {kind} job. Use {allowed}."
        )
    return text


def require_api_key() -> str:
    from calliope.config import settings

    if not settings.comfy_router_enabled:
        raise RuntimeError(
            "Comfy Router is turned off in Settings → ComfyUI. "
            "Turn on 'Use Comfy API provider', or generate with a local workflow."
        )
    key = (settings.comfy_api_key or "").strip()
    if not key:
        raise RuntimeError(
            "Comfy Router needs a Comfy API key. Add one in Settings → ComfyUI. "
            "Local workflow jobs do not need a key."
        )
    return key


def first_local_image(*candidates: Any) -> str | None:
    for value in candidates:
        if isinstance(value, str) and Path(value).suffix.lower() in _IMAGE_EXT:
            if Path(value).is_file():
                return value
    return None


def first_image_in_values(values: dict[str, Any] | None) -> str | None:
    if not values:
        return None
    return first_local_image(*values.values())


_MAX_EDGE = 1280


def user_asked_h3_router(text: str | None) -> bool:
    """True when the user's own words asked for Comfy Router MiniMax H3."""
    low = (text or "").lower()
    if "minimax/minimax-h3" in low:
        return True
    has_router = "comfy router" in low or "router" in low
    has_h3 = "minimax h3" in low or "minimax-h3" in low
    return has_router and has_h3


def normalize_router_images(raw: Any) -> list[dict[str, str]]:
    """Existing local files, in order, capped at 9. Each item is ``{path, name}``."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if isinstance(item, str):
            path, name = item, ""
        elif isinstance(item, dict):
            path = str(item.get("path") or "")
            name = str(item.get("name") or "")
        else:
            continue
        path = path.strip()
        if not path or not Path(path).is_file():
            continue
        label = name.strip() or Path(path).stem or "reference"
        out.append({"path": path, "name": label})
        if len(out) >= 9:
            break
    return out


def _encoded_image(path: str) -> tuple[bytes, str]:
    """Bytes for a data URI. A long edge above 1280px is re-encoded as JPEG."""
    file = Path(path)
    raw = file.read_bytes()
    mime = _MIME.get(file.suffix.lower(), "application/octet-stream")
    try:
        from PIL import Image

        with Image.open(file) as im:
            width, height = im.size
            if max(width, height) <= _MAX_EDGE:
                return raw, mime
            scale = _MAX_EDGE / max(width, height)
            resized = im.convert("RGB").resize(
                (max(1, int(width * scale)), max(1, int(height * scale))),
                Image.Resampling.LANCZOS,
            )
            import io

            buf = io.BytesIO()
            resized.save(buf, format="JPEG", quality=85)
            return buf.getvalue(), "image/jpeg"
    except Exception:
        return raw, mime


def _data_uri(path: str) -> str:
    data, mime = _encoded_image(path)
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _raw_base64(path: str) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


def h3_duration(seconds: Any) -> int:
    """Router accepts 4–15 seconds. Missing or junk values become 5."""
    try:
        value = int(round(float(seconds)))
    except (TypeError, ValueError):
        return 5
    return max(4, min(15, value))


def build_h3_body(
    prompt: str,
    duration: Any,
    images: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Reference-to-video body. Images are ``reference_image`` parts, in order."""
    text = (prompt or "").strip()
    if not text:
        raise RuntimeError("Comfy Router MiniMax H3 job has no prompt.")
    refs = normalize_router_images(images or [])
    if refs:
        lines = [f"Image {index} is {item['name']}." for index, item in enumerate(refs, start=1)]
        text = text + "\n\n" + "\n".join(lines)
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for item in refs:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": _data_uri(item["path"])},
                "role": "reference_image",
            }
        )
    return {
        "content": content,
        "duration": h3_duration(duration),
        "ratio": "adaptive" if refs else "16:9",
        "resolution": "768P",
    }


def build_flux_body(prompt: str, image_path: str | None) -> dict[str, Any]:
    text = (prompt or "").strip()
    if not text:
        raise RuntimeError("Comfy Router FLUX 3 job has no prompt.")
    body: dict[str, Any] = {
        "prompt": text,
        "aspect_ratio": "16:9",
        "resolution": "1k",
    }
    if image_path and Path(image_path).is_file():
        body["images"] = _raw_base64(image_path)
    return body


def build_router_body(model: str, payload: dict[str, Any]) -> dict[str, Any]:
    prompt = str(payload.get("prompt") or "")
    if model == ROUTER_VIDEO_MODEL:
        images = normalize_router_images(payload.get("router_images"))
        if not images:
            one = first_local_image(payload.get("router_image"))
            if one:
                images = [{"path": one, "name": Path(one).stem or "reference"}]
        return build_h3_body(prompt, payload.get("duration_sec"), images)
    if model == ROUTER_IMAGE_MODEL:
        image = first_local_image(payload.get("router_image")) or first_image_in_values(
            payload.get("input_values") if isinstance(payload.get("input_values"), dict) else None
        )
        return build_flux_body(prompt, image)
    raise RuntimeError(f"Unknown Comfy Router model {model!r}.")


def result_urls(payload: Any) -> list[str]:
    """Partner result URLs, in document order. H3 uses task.content.url; FLUX uses result.sample."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"url", "sample"} and isinstance(value, str) and value.startswith("http"):
                    found.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return found


def result_filename(url: str, kind: str) -> str:
    name = Path(url.split("?", 1)[0]).name
    if name and "." in name:
        return name
    return "router-output.mp4" if kind == "video" else "router-output.png"


async def download_url(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as http:
        resp = await http.get(url)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
