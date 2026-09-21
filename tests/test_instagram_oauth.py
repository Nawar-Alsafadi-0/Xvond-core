from pathlib import Path
from urllib.parse import parse_qs, urlparse

from backend.app.core.config.settings import settings
from backend.app.modules.channels import instagram_oauth
from backend.app.modules.channels.instagram_oauth import (
    INSTAGRAM_SCOPES,
    build_instagram_authorization_url,
    issue_instagram_oauth_state,
    subscribe_instagram_messaging,
    verify_instagram_oauth_state,
)


ROOT = Path(__file__).resolve().parents[1]


def test_instagram_oauth_state_round_trip(monkeypatch):
    monkeypatch.setattr(settings, "GENERIC_OAUTH_STATE_SECRET", "test-instagram-oauth-secret-1234567890")
    state = issue_instagram_oauth_state(user_id=7, company_id=11, agent_id=13)
    payload = verify_instagram_oauth_state(state)
    assert payload["user_id"] == 7
    assert payload["company_id"] == 11
    assert payload["agent_id"] == 13
    assert payload["nonce"]


def test_instagram_authorization_url_uses_direct_business_login(monkeypatch):
    monkeypatch.setattr(settings, "META_INSTAGRAM_APP_ID", "123456")
    monkeypatch.setattr(settings, "META_INSTAGRAM_APP_SECRET", "secret")
    monkeypatch.setattr(settings, "META_INSTAGRAM_REDIRECT_URI", "https://xvond.com/customer/meta/channels/instagram/oauth/callback")
    monkeypatch.setattr(settings, "GENERIC_OAUTH_STATE_SECRET", "test-instagram-oauth-secret-1234567890")

    url = build_instagram_authorization_url(state="signed-state")
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "www.instagram.com"
    assert parsed.path == "/oauth/authorize"
    assert query["client_id"] == ["123456"]
    assert query["redirect_uri"] == ["https://xvond.com/customer/meta/channels/instagram/oauth/callback"]
    assert query["response_type"] == ["code"]
    assert query["state"] == ["signed-state"]
    assert set(query["scope"][0].split(",")) == set(INSTAGRAM_SCOPES)
    assert query["enable_fb_login"] == ["0"]


def test_customer_portal_starts_direct_instagram_oauth():
    javascript = (ROOT / "frontend/customer/meta-channels.js").read_text(encoding="utf-8-sig")
    assert 'type === "instagram"' in javascript
    assert '"/customer/meta/channels/instagram/oauth/start"' in javascript
    assert "window.location.assign(result.authorization_url)" in javascript


def test_instagram_subscription_uses_documented_query_parameters(monkeypatch):
    monkeypatch.setattr(settings, "META_GRAPH_API_VERSION", "v26.0")
    captured = {}

    def fake_request_json(method, url, **kwargs):
        captured.update({"method": method, "url": url, "kwargs": kwargs})
        return {"success": True}

    monkeypatch.setattr(instagram_oauth, "_request_json", fake_request_json)

    subscribe_instagram_messaging(user_id="17841400000000000", access_token="test-token")

    parsed = urlparse(captured["url"])
    query = parse_qs(parsed.query)
    assert captured["method"] == "POST"
    assert parsed.scheme == "https"
    assert parsed.netloc == "graph.instagram.com"
    assert parsed.path == "/v26.0/17841400000000000/subscribed_apps"
    assert query == {
        "subscribed_fields": ["messages,messaging_postbacks"],
        "access_token": ["test-token"],
    }
    assert captured["kwargs"] == {}
