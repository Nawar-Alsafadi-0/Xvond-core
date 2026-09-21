from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from backend.app.core.config.settings import settings


class InstagramOAuthError(RuntimeError):
    pass


INSTAGRAM_SCOPES = (
    "instagram_business_basic",
    "instagram_business_manage_messages",
)


def _redirect_uri() -> str:
    configured = str(settings.META_INSTAGRAM_REDIRECT_URI or "").strip()
    if configured:
        return configured
    base = str(settings.PUBLIC_BASE_URL or "").strip().rstrip("/")
    return f"{base}/customer/meta/channels/instagram/oauth/callback" if base else ""


def instagram_oauth_ready() -> bool:
    return bool(
        settings.META_INSTAGRAM_APP_ID
        and settings.META_INSTAGRAM_APP_SECRET
        and _redirect_uri()
        and settings.GENERIC_OAUTH_STATE_SECRET
    )


def _state_secret() -> bytes:
    value = str(settings.GENERIC_OAUTH_STATE_SECRET or "").strip()
    if not value:
        raise InstagramOAuthError("Instagram OAuth state signing is not configured")
    return value.encode("utf-8")


def issue_instagram_oauth_state(*, user_id: int, company_id: int, agent_id: int) -> str:
    payload = {
        "user_id": int(user_id),
        "company_id": int(company_id),
        "agent_id": int(agent_id),
        "nonce": secrets.token_urlsafe(24),
        "exp": int(time.time()) + 600,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    signature = hmac.new(_state_secret(), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def verify_instagram_oauth_state(state: str) -> dict:
    value = str(state or "").strip()
    if "." not in value:
        raise InstagramOAuthError("Invalid Instagram connection state")
    encoded, supplied = value.rsplit(".", 1)
    expected = hmac.new(_state_secret(), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied):
        raise InstagramOAuthError("Invalid Instagram connection state")
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception as exc:
        raise InstagramOAuthError("Invalid Instagram connection state") from exc
    if int(payload.get("exp") or 0) < int(time.time()):
        raise InstagramOAuthError("Instagram connection session expired")
    if not all(int(payload.get(key) or 0) > 0 for key in ("user_id", "company_id", "agent_id")):
        raise InstagramOAuthError("Invalid Instagram connection state")
    if not str(payload.get("nonce") or "").strip():
        raise InstagramOAuthError("Invalid Instagram connection state")
    return payload


def build_instagram_authorization_url(*, state: str) -> str:
    if not instagram_oauth_ready():
        raise InstagramOAuthError("Instagram OAuth is not configured")
    query = urllib.parse.urlencode(
        {
            "client_id": settings.META_INSTAGRAM_APP_ID,
            "redirect_uri": _redirect_uri(),
            "response_type": "code",
            "scope": ",".join(INSTAGRAM_SCOPES),
            "state": state,
            "enable_fb_login": "0",
            "force_authentication": "1",
        }
    )
    return f"https://www.instagram.com/oauth/authorize?{query}"


def _request_json(
    method: str,
    url: str,
    *,
    form: dict | None = None,
    access_token: str | None = None,
) -> dict:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"api.instagram.com", "graph.instagram.com"}:
        raise InstagramOAuthError("Invalid Instagram API destination")

    headers = {"Accept": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    body = None
    if form is not None:
        body = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    request = urllib.request.Request(url=url, data=body, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:500]
        except Exception:
            pass
        raise InstagramOAuthError(
            f"Instagram API rejected the request (HTTP {exc.code})"
            + (f": {detail}" if detail else "")
        ) from exc
    except Exception as exc:
        if isinstance(exc, InstagramOAuthError):
            raise
        raise InstagramOAuthError("Instagram API request failed") from exc


def exchange_instagram_code(*, code: str) -> dict:
    redirect_uri = _redirect_uri()
    try:
        short = _request_json(
            "POST",
            "https://api.instagram.com/oauth/access_token",
            form={
                "client_id": settings.META_INSTAGRAM_APP_ID,
                "client_secret": settings.META_INSTAGRAM_APP_SECRET,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
                "code": str(code or "").strip(),
            },
        )
    except InstagramOAuthError as exc:
        raise InstagramOAuthError(f"Instagram code exchange failed: {exc}") from exc
    short_token = str(short.get("access_token") or "").strip()
    if not short_token:
        raise InstagramOAuthError("Instagram did not return an access token")

    version = str(settings.META_GRAPH_API_VERSION or "v26.0").strip()
    long_url = "https://graph.instagram.com/access_token?" + urllib.parse.urlencode(
        {
            "grant_type": "ig_exchange_token",
            "client_secret": settings.META_INSTAGRAM_APP_SECRET,
            "access_token": short_token,
        }
    )
    token_type = "long_lived"
    try:
        long = _request_json("GET", long_url)
        access_token = str(long.get("access_token") or "").strip() or short_token
        expires_in = long.get("expires_in")
    except InstagramOAuthError as exc:
        detail = str(exc)
        known_meta_method_rejection = (
            "Unsupported request - method type: get" in detail
            and "IGApiException" in detail
            and ('"code":100' in detail or '"code": 100' in detail)
        )
        if not known_meta_method_rejection:
            raise InstagramOAuthError(f"Instagram long-lived token exchange failed: {exc}") from exc
        # Meta has intermittently rejected the documented long-lived-token GET
        # with IGApiException code 100. Keep the successful short-lived token so
        # the connection can be verified end-to-end, but expose the degraded
        # token type to callers. The token is never returned to the browser.
        long = {}
        access_token = short_token
        expires_in = short.get("expires_in")
        token_type = "short_lived_fallback"

    profile_url = (
        f"https://graph.instagram.com/{version}/me?"
        + urllib.parse.urlencode({"fields": "user_id,username", "access_token": access_token})
    )
    try:
        profile = _request_json("GET", profile_url)
    except InstagramOAuthError as exc:
        raise InstagramOAuthError(f"Instagram profile lookup failed: {exc}") from exc
    user_id = str(profile.get("user_id") or profile.get("id") or short.get("user_id") or "").strip()
    username = str(profile.get("username") or "").strip()
    if not user_id:
        raise InstagramOAuthError("Instagram did not return the professional account ID")

    return {
        "access_token": access_token,
        "user_id": user_id,
        "username": username,
        "expires_in": expires_in,
        "token_type": token_type,
    }


def subscribe_instagram_messaging(*, user_id: str, access_token: str) -> None:
    version = str(settings.META_GRAPH_API_VERSION or "v26.0").strip()
    url = (
        f"https://graph.instagram.com/{version}/"
        f"{urllib.parse.quote(str(user_id), safe='')}/subscribed_apps?"
        + urllib.parse.urlencode(
            {
                "subscribed_fields": "messages,messaging_postbacks",
                "access_token": access_token,
            }
        )
    )
    try:
        payload = _request_json("POST", url)
    except InstagramOAuthError as exc:
        raise InstagramOAuthError(f"Instagram webhook subscription failed: {exc}") from exc
    if payload.get("success") is not True:
        raise InstagramOAuthError("Instagram did not confirm the messaging webhook subscription")
