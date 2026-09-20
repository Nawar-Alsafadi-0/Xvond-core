from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlencode

from backend.app.core.http_security import safe_http_request, validate_public_http_url


STATE_TTL_SECONDS = 600


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def create_oauth_authorization(
    flow: dict,
    *,
    client_id: str,
    redirect_uri: str,
    state_secret: str,
    company_id: int,
    agent_id: int,
    requirement_key: str,
    integration_id: int | None = None,
) -> dict:
    if not state_secret:
        raise ValueError("OAuth state signing secret is not configured")
    if str(flow.get("flow") or "") != "authorization_code":
        raise ValueError("OAuth authorization-code flow is not available")
    authorization_url = validate_public_http_url(str(flow.get("authorization_url") or ""))
    token_url = validate_public_http_url(str(flow.get("token_url") or ""))
    if not authorization_url.startswith("https://") or not token_url.startswith("https://"):
        raise ValueError("OAuth endpoints must use HTTPS")
    redirect_uri = str(redirect_uri or "").strip()
    if not redirect_uri.startswith("https://") and not redirect_uri.startswith("http://localhost"):
        raise ValueError("OAuth redirect URI must use HTTPS")

    verifier = _b64(secrets.token_bytes(48))
    challenge = _b64(hashlib.sha256(verifier.encode("ascii")).digest())
    nonce = secrets.token_urlsafe(24)
    payload = {
        "v": 1,
        "company_id": int(company_id),
        "agent_id": int(agent_id),
        "requirement_key": str(requirement_key),
        "integration_id": int(integration_id) if integration_id is not None else None,
        "nonce": nonce,
        "exp": int(time.time()) + STATE_TTL_SECONDS,
        "token_url": token_url,
        "redirect_uri": redirect_uri,
        "client_id": str(client_id),
    }
    encoded = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = _b64(hmac.new(state_secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest())
    state = f"{encoded}.{signature}"
    params = {
        "response_type": "code",
        "client_id": str(client_id),
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    scopes = [str(item).strip() for item in (flow.get("scopes") or []) if str(item).strip()][:50]
    if scopes:
        params["scope"] = " ".join(scopes)
    separator = "&" if "?" in authorization_url else "?"
    return {
        "authorization_url": authorization_url + separator + urlencode(params),
        "state": state,
        "code_verifier": verifier,
        "expires_in": STATE_TTL_SECONDS,
    }


def consume_oauth_state(state: str, *, state_secret: str) -> dict:
    try:
        encoded, signature = str(state).split(".", 1)
        expected = _b64(hmac.new(state_secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise ValueError("OAuth state signature is invalid")
        payload = json.loads(_unb64(encoded).decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("OAuth state is invalid") from exc
    if int(payload.get("exp") or 0) < int(time.time()):
        raise ValueError("OAuth state has expired")
    return payload


def exchange_authorization_code(
    *,
    state_payload: dict,
    code: str,
    client_secret: str,
    code_verifier: str,
) -> dict:
    if not str(code_verifier or "").strip():
        raise ValueError("OAuth PKCE verifier is missing")
    result = safe_http_request(
        url=str(state_payload.get("token_url") or ""),
        method="POST",
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        form_data={
            "grant_type": "authorization_code",
            "code": str(code),
            "redirect_uri": str(state_payload.get("redirect_uri") or ""),
            "client_id": str(state_payload.get("client_id") or ""),
            "client_secret": str(client_secret),
            "code_verifier": str(code_verifier or ""),
        },
        timeout=15,
        max_response_bytes=64_000,
    )
    status = int(result.get("status_code") or 0)
    if not 200 <= status < 300:
        raise ValueError(f"OAuth token endpoint returned HTTP {status}")
    try:
        payload = json.loads(str(result.get("response") or ""))
    except json.JSONDecodeError as exc:
        raise ValueError("OAuth token response is not valid JSON") from exc
    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        raise ValueError("OAuth token response has no access_token")
    if str(payload.get("token_type") or "Bearer").lower() != "bearer":
        raise ValueError("Only Bearer OAuth access tokens are supported")
    return {
        "access_token": access_token,
        "refresh_token": str(payload.get("refresh_token") or "").strip() or None,
        "expires_in": payload.get("expires_in"),
        "scope": payload.get("scope"),
        "token_type": "Bearer",
    }
