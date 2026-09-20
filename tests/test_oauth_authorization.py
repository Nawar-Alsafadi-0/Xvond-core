import pytest

from backend.app.modules.integrations import oauth_authorization as oauth


def test_authorization_uses_signed_state_and_pkce(monkeypatch):
    monkeypatch.setattr(oauth, "validate_public_http_url", lambda value: value)
    result = oauth.create_oauth_authorization(
        {
            "flow": "authorization_code",
            "authorization_url": "https://accounts.example.com/oauth/authorize",
            "token_url": "https://accounts.example.com/oauth/token",
            "scopes": ["orders.read"],
        },
        client_id="client-1",
        redirect_uri="https://xvond.example/oauth/callback",
        state_secret="state-secret",
        company_id=7,
        agent_id=9,
        requirement_key="orders_api",
    )
    assert "code_challenge_method=S256" in result["authorization_url"]
    assert "scope=orders.read" in result["authorization_url"]
    payload = oauth.consume_oauth_state(result["state"], state_secret="state-secret")
    assert payload["company_id"] == 7
    assert payload["agent_id"] == 9
    assert payload["requirement_key"] == "orders_api"
    assert payload["code_verifier"]


def test_tampered_oauth_state_is_rejected(monkeypatch):
    monkeypatch.setattr(oauth, "validate_public_http_url", lambda value: value)
    result = oauth.create_oauth_authorization(
        {
            "flow": "authorization_code",
            "authorization_url": "https://accounts.example.com/oauth/authorize",
            "token_url": "https://accounts.example.com/oauth/token",
        },
        client_id="client-1",
        redirect_uri="https://xvond.example/oauth/callback",
        state_secret="state-secret",
        company_id=1,
        agent_id=2,
        requirement_key="api",
    )
    with pytest.raises(ValueError, match="state"):
        oauth.consume_oauth_state(result["state"] + "x", state_secret="state-secret")


def test_exchange_sends_pkce_verifier(monkeypatch):
    captured = {}
    def fake_http(**kwargs):
        captured.update(kwargs)
        return {
            "status_code": 200,
            "response": '{"access_token":"token","refresh_token":"refresh","token_type":"Bearer"}',
        }
    monkeypatch.setattr(oauth, "safe_http_request", fake_http)
    token = oauth.exchange_authorization_code(
        state_payload={
            "token_url": "https://accounts.example.com/oauth/token",
            "redirect_uri": "https://xvond.example/oauth/callback",
            "client_id": "client-1",
            "code_verifier": "verifier",
        },
        code="code-1",
        client_secret="secret-1",
    )
    assert token["access_token"] == "token"
    assert token["refresh_token"] == "refresh"
    assert captured["form_data"]["code_verifier"] == "verifier"
    assert captured["method"] == "POST"
