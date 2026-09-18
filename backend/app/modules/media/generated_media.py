from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

import httpx

from backend.app.core.config.settings import settings


class MediaGenerationError(RuntimeError):
    pass


def _storage_dir() -> Path:
    path = Path(settings.MEDIA_STORAGE_DIR).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_filename(filename: str) -> str:
    value = str(filename or "").strip()
    if not value or "/" in value or "\\" in value or value.startswith("."):
        raise MediaGenerationError("Invalid generated media filename")
    if not value.endswith((".png", ".jpg", ".jpeg", ".webp")):
        raise MediaGenerationError("Unsupported generated media format")
    return value


def _signature(filename: str, expires: int) -> str:
    payload = f"{filename}:{int(expires)}".encode("utf-8")
    return hmac.new(
        settings.JWT_SECRET.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()


def signed_media_url(filename: str, *, ttl_seconds: int | None = None) -> str:
    filename = _safe_filename(filename)
    if not settings.PUBLIC_BASE_URL:
        raise MediaGenerationError("PUBLIC_BASE_URL is required for public generated media")
    ttl = int(ttl_seconds or settings.MEDIA_PUBLIC_TTL_SECONDS)
    expires = int(time.time()) + max(300, min(ttl, 86400))
    sig = _signature(filename, expires)
    return (
        f"{settings.PUBLIC_BASE_URL}/media/generated/{filename}"
        f"?expires={expires}&sig={sig}"
    )


def resolve_signed_media(filename: str, *, expires: int, sig: str) -> Path:
    filename = _safe_filename(filename)
    if int(expires) < int(time.time()):
        raise MediaGenerationError("Generated media URL has expired")
    expected = _signature(filename, int(expires))
    if not hmac.compare_digest(str(sig or ""), expected):
        raise MediaGenerationError("Generated media signature is invalid")
    path = (_storage_dir() / filename).resolve()
    if path.parent != _storage_dir() or not path.is_file():
        raise MediaGenerationError("Generated media not found")
    return path


def _cleanup_old_assets() -> None:
    cutoff = time.time() - max(settings.MEDIA_PUBLIC_TTL_SECONDS * 2, 86400)
    try:
        for item in _storage_dir().iterdir():
            if item.is_file() and item.stat().st_mtime < cutoff:
                item.unlink(missing_ok=True)
    except OSError:
        return


def generate_image_asset(
    *,
    prompt: str,
    model: str | None = None,
    size: str = "1024x1024",
) -> dict:
    if not settings.OPENAI_API_KEY:
        raise MediaGenerationError("OpenAI image generation is not configured")
    clean_prompt = str(prompt or "").strip()
    if not clean_prompt:
        raise MediaGenerationError("Image generation prompt is required")
    clean_prompt = clean_prompt[:32000]

    payload = {
        "model": str(model or settings.IMAGE_GENERATION_MODEL),
        "prompt": clean_prompt,
        "size": str(size or "1024x1024"),
        "quality": "auto",
        "output_format": "jpeg",
        "n": 1,
    }
    try:
        response = httpx.post(
            "https://api.openai.com/v1/images/generations",
            headers={
                "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=120.0,
        )
    except httpx.HTTPError as exc:
        raise MediaGenerationError("Image generation provider request failed") from exc

    if response.status_code >= 400:
        raise MediaGenerationError(
            f"Image generation provider returned HTTP {response.status_code}"
        )
    try:
        body = response.json()
        encoded = str((body.get("data") or [])[0].get("b64_json") or "")
    except (ValueError, IndexError, AttributeError, TypeError) as exc:
        raise MediaGenerationError("Image generation provider returned invalid data") from exc
    if not encoded:
        raise MediaGenerationError("Image generation provider returned no image")

    try:
        raw = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise MediaGenerationError("Generated image payload is invalid") from exc
    if not raw or len(raw) > 25_000_000:
        raise MediaGenerationError("Generated image size is invalid")

    _cleanup_old_assets()
    filename = f"{secrets.token_hex(24)}.jpg"
    path = _storage_dir() / filename
    tmp = _storage_dir() / f".{filename}.tmp"
    try:
        tmp.write_bytes(raw)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)

    return {
        "filename": filename,
        "media_url": signed_media_url(filename),
        "content_type": "image/jpeg",
        "bytes": len(raw),
        "model": payload["model"],
    }
