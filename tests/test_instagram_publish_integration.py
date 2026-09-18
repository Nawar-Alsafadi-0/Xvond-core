from backend.app.modules.tools import action_request as runtime


def test_instagram_publish_creates_container_then_publishes(monkeypatch):
    calls = []

    monkeypatch.setattr(runtime, "validate_public_http_url", lambda url: url)

    def fake_request(**kwargs):
        calls.append(kwargs)
        if kwargs["url"].endswith("/media"):
            return {
                "status_code": 200,
                "response": '{"id":"creation-123"}',
                "truncated": False,
            }
        if kwargs["url"].endswith("/media_publish"):
            return {
                "status_code": 200,
                "response": '{"id":"media-456"}',
                "truncated": False,
            }
        raise AssertionError(kwargs["url"])

    monkeypatch.setattr(runtime, "safe_http_request", fake_request)

    result = runtime._instagram_publish_call(
        config={
            "instagram_user_id": "178414000",
            "access_token": "secret-token",
        },
        payload={
            "details": {
                "image_url": "https://cdn.example.com/post.jpg",
                "ai_response": "Generated caption",
            }
        },
        operation="execute",
    )

    assert result.success is True
    assert result.data["creation_id"] == "creation-123"
    assert result.data["media_id"] == "media-456"

    assert calls[0]["url"] == "https://graph.facebook.com/178414000/media"
    assert calls[0]["headers"]["Authorization"] == "Bearer secret-token"
    assert calls[0]["form_data"] == {
        "image_url": "https://cdn.example.com/post.jpg",
        "caption": "Generated caption",
    }
    assert calls[1]["url"] == "https://graph.facebook.com/178414000/media_publish"
    assert calls[1]["form_data"] == {"creation_id": "creation-123"}


def test_instagram_publish_requires_real_media_url(monkeypatch):
    monkeypatch.setattr(
        runtime,
        "safe_http_request",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("provider must not be called without media")
        ),
    )

    result = runtime._instagram_publish_call(
        config={
            "instagram_user_id": "178414000",
            "access_token": "secret-token",
        },
        payload={"details": {"ai_response": "Caption only"}},
        operation="execute",
    )

    assert result.success is False
    assert "requires image_url or media_url" in result.error


def test_instagram_publish_does_not_claim_success_when_container_fails(monkeypatch):
    monkeypatch.setattr(runtime, "validate_public_http_url", lambda url: url)
    monkeypatch.setattr(
        runtime,
        "safe_http_request",
        lambda **kwargs: {
            "status_code": 400,
            "response": '{"error":{"message":"bad media"}}',
            "truncated": False,
        },
    )

    result = runtime._instagram_publish_call(
        config={
            "instagram_user_id": "178414000",
            "access_token": "secret-token",
        },
        payload={
            "details": {
                "image_url": "https://cdn.example.com/post.jpg",
                "caption": "Post",
            }
        },
        operation="execute",
    )

    assert result.success is False
    assert "HTTP 400" in result.error
