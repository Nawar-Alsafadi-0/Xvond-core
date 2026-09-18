from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt

from backend.app.core.config.settings import settings


OAUTH_STATE_AUDIENCE = "xvond-integration-oauth"
OAUTH_STATE_TTL_MINUTES = 10


def create_integration_oauth_state(
    *,
    company_id: int,
    user_id: int,
    provider: str,
    integration_id: int | None = None,
) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(int(user_id)),
        "company_id": int(company_id),
        "provider": str(provider or "").strip().lower(),
        "integration_id": (
            int(integration_id) if integration_id is not None else None
        ),
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(minutes=OAUTH_STATE_TTL_MINUTES),
        "iss": settings.JWT_ISSUER,
        "aud": OAUTH_STATE_AUDIENCE,
        "jti": uuid4().hex,
    }
    if not payload["provider"]:
        raise ValueError("OAuth provider is required")
    return jwt.encode(
        payload,
        settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM,
    )


def decode_integration_oauth_state(token: str) -> dict:
    payload = jwt.decode(
        str(token or ""),
        settings.JWT_SECRET,
        algorithms=[settings.JWT_ALGORITHM],
        issuer=settings.JWT_ISSUER,
        audience=OAUTH_STATE_AUDIENCE,
        options={
            "require": [
                "sub",
                "company_id",
                "provider",
                "iat",
                "nbf",
                "exp",
                "iss",
                "aud",
                "jti",
            ]
        },
    )
    return {
        **payload,
        "user_id": int(payload["sub"]),
        "company_id": int(payload["company_id"]),
        "integration_id": (
            int(payload["integration_id"])
            if payload.get("integration_id") is not None
            else None
        ),
        "provider": str(payload["provider"] or "").strip().lower(),
        "jti": str(payload["jti"]),
    }
