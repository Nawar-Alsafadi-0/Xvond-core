import base64
import time

from backend.app.modules.automation import runtime as automation_runtime_module
from backend.app.modules.media import generated_media


class _ImageResponse:
    status_code = 200

    def json(self):
        return {
            "data": [
                {
                    "b64_json": base64.b64encode(b"fake-jpeg-bytes").decode("ascii"),
                }
            ]
        }


def test_generated_media_asset_is_persisted_and_signed(monkeypatch, tmp_path):
    monkeypatch.setattr(generated_media.settings, "MEDIA_STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(generated_media.settings, "PUBLIC_BASE_URL", "https://api.xvond.test")
    monkeypatch.setattr(generated_media.settings, "JWT_SECRET", "test-secret-" * 4)
    monkeypatch.setattr(generated_media.settings, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(generated_media.settings, "IMAGE_GENERATION_MODEL", "gpt-image-2.5-flare")
    monkeypatch.setattr(generated_media.httpx, "post", lambda *args, **kwargs: _ImageResponse())

    asset = generated_media.generate_image_asset(prompt="Create a clean product visual")

    assert asset["content_type"] == "image/jpeg"
    assert asset["bytes"] == len(b"fake-jpeg-bytes")
    assert asset["model"] == "gpt-image-2.5-flare"
    assert asset["media_url"].startswith(
        f"https://api.xvond.test/media/generated/{asset['filename']}?"
    )
    assert (tmp_path / asset["filename"]).read_bytes() == b"fake-jpeg-bytes"

    query = asset["media_url"].split("?", 1)[1]
    values = dict(item.split("=", 1) for item in query.split("&"))
    resolved = generated_media.resolve_signed_media(
        asset["filename"],
        expires=int(values["expires"]),
        sig=values["sig"],
    )
    assert resolved == tmp_path / asset["filename"]


def test_generated_media_rejects_expired_or_bad_signature(monkeypatch, tmp_path):
    monkeypatch.setattr(generated_media.settings, "MEDIA_STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(generated_media.settings, "JWT_SECRET", "test-secret-" * 4)
    filename = "abc123.jpg"
    (tmp_path / filename).write_bytes(b"x")

    try:
        generated_media.resolve_signed_media(
            filename,
            expires=int(time.time()) - 1,
            sig="bad",
        )
    except generated_media.MediaGenerationError as exc:
        assert "expired" in str(exc).lower()
    else:
        raise AssertionError("expired generated media URL must fail")


def test_media_generation_step_passes_prior_ai_content_to_generator(monkeypatch):
    captured = {}

    def fake_generate_image_asset(*, prompt, model=None, size="1024x1024"):
        captured.update({"prompt": prompt, "model": model, "size": size})
        return {
            "media_url": "https://api.xvond.test/media/generated/example.jpg?expires=1&sig=x",
            "content_type": "image/jpeg",
            "bytes": 123,
            "model": "gpt-image-2.5-flare",
        }

    monkeypatch.setattr(
        automation_runtime_module,
        "generate_image_asset",
        fake_generate_image_asset,
    )

    result = automation_runtime_module.automation_runtime.execute_step(
        db=object(),
        company_id=1,
        step={
            "type": "media_generation",
            "prompt": "Create the publishable visual.",
            "size": "1024x1024",
        },
        state={"ai_response": "Launch post about our new clinic service."},
        run_id=1,
        step_index=1,
    )

    assert "Create the publishable visual." in captured["prompt"]
    assert "Launch post about our new clinic service." in captured["prompt"]
    assert result["media_url"].startswith("https://api.xvond.test/media/generated/")
    assert result["generated_media"]["bytes"] == 123
