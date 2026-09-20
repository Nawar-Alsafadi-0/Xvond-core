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
    assert "code_verifier" not in payload
    assert result["code_verifier"]


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
        code_verifier="verifier",
    )
    assert token["access_token"] == "token"
    assert token["refresh_token"] == "refresh"
    assert captured["form_data"]["code_verifier"] == "verifier"
    assert captured["method"] == "POST"


def test_authorization_state_binds_pending_integration(monkeypatch):
    monkeypatch.setattr(oauth, "validate_public_http_url", lambda value: value)
    result = oauth.create_oauth_authorization(
        {
            "flow": "authorization_code",
            "authorization_url": "https://accounts.example.com/oauth/authorize",
            "token_url": "https://accounts.example.com/oauth/token",
        },
        client_id="client-1",
        redirect_uri="https://xvond.example/customer/employee-builder/oauth/callback",
        state_secret="state-secret",
        company_id=3,
        agent_id=4,
        requirement_key="orders_api",
        integration_id=55,
    )
    payload = oauth.consume_oauth_state(result["state"], state_secret="state-secret")
    assert payload["integration_id"] == 55


def test_exchange_rejects_missing_pkce_verifier():
    with pytest.raises(ValueError, match="PKCE verifier"):
        oauth.exchange_authorization_code(
            state_payload={
                "token_url": "https://accounts.example.com/oauth/token",
                "redirect_uri": "https://xvond.example/oauth/callback",
                "client_id": "client-1",
            },
            code="code-1",
            client_secret="secret-1",
            code_verifier="",
        )


def test_oauth_token_timing_and_refresh_window():
    timing = oauth.oauth_token_timing(3600, now_epoch=1_000)
    assert timing == {"obtained_at": 1_000, "expires_at": 4_600}
    assert oauth.oauth_access_token_needs_refresh(
        {"flow": "authorization_code", "expires_at": 1_050},
        now_epoch=1_000,
        skew_seconds=60,
    ) is True
    assert oauth.oauth_access_token_needs_refresh(
        {"flow": "authorization_code", "expires_at": 1_500},
        now_epoch=1_000,
        skew_seconds=60,
    ) is False
    assert oauth.oauth_access_token_needs_refresh(
        {"flow": "client_credentials", "expires_at": 1_050},
        now_epoch=1_000,
        skew_seconds=60,
    ) is True
    assert oauth.oauth_access_token_needs_refresh(
        {
            "flow": "client_credentials",
            "token_url": "https://accounts.example.com/oauth/token",
        },
        now_epoch=1_000,
    ) is True
    assert oauth.oauth_access_token_needs_refresh(
        {
            "flow": "client_credentials",
            "obtained_at": 900,
        },
        now_epoch=1_000,
    ) is False
    assert oauth.oauth_access_token_needs_refresh(
        {
            "flow": "authorization_code",
            "expires_in": 3600,
            "refresh_token": "legacy-refresh",
        },
        now_epoch=1_000,
    ) is True


def test_refresh_oauth_access_token_rotates_refresh_token(monkeypatch):
    captured = {}
    monkeypatch.setattr(oauth, "validate_public_http_url", lambda value: value)

    def fake_http(**kwargs):
        captured.update(kwargs)
        return {
            "status_code": 200,
            "response": (
                '{"access_token":"access-2","refresh_token":"refresh-2",'
                '"expires_in":1800,"token_type":"Bearer"}'
            ),
        }

    monkeypatch.setattr(oauth, "safe_http_request", fake_http)
    token = oauth.refresh_oauth_access_token(
        {
            "flow": "authorization_code",
            "token_url": "https://accounts.example.com/oauth/token",
            "client_id": "client-1",
            "client_secret": "secret-1",
            "refresh_token": "refresh-1",
        }
    )
    assert token["access_token"] == "access-2"
    assert token["refresh_token"] == "refresh-2"
    assert token["expires_in"] == 1800
    assert captured["form_data"]["grant_type"] == "refresh_token"
    assert captured["form_data"]["refresh_token"] == "refresh-1"


def test_refresh_oauth_access_token_preserves_refresh_token_when_omitted(monkeypatch):
    monkeypatch.setattr(oauth, "validate_public_http_url", lambda value: value)
    monkeypatch.setattr(
        oauth,
        "safe_http_request",
        lambda **kwargs: {
            "status_code": 200,
            "response": '{"access_token":"access-2","token_type":"Bearer"}',
        },
    )
    token = oauth.refresh_oauth_access_token(
        {
            "flow": "authorization_code",
            "token_url": "https://accounts.example.com/oauth/token",
            "client_id": "client-1",
            "client_secret": "secret-1",
            "refresh_token": "refresh-1",
        }
    )
    assert token["refresh_token"] == "refresh-1"
