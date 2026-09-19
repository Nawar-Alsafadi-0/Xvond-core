from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
from contextlib import contextmanager
from urllib.parse import urlsplit

import psycopg
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

app = FastAPI(title="Xvond Workflow Registry", docs_url=None, redoc_url=None, openapi_url=None)

MAX_PROVIDER_CONFIG_BYTES = 65_536
PROVIDER_CHANNELS = {
    "telegram": {"telegram"},
    "instagram": {"instagram"},
    "messenger": {"messenger"},
    "meta": {"instagram", "messenger"},
    "slack": {"slack"},
    "sms": {"sms"},
    "custom": {"custom"},
}


def _secret() -> str:
    value = os.getenv("WORKFLOW_REGISTRY_SHARED_SECRET", "")
    if len(value) < 32:
        raise RuntimeError("WORKFLOW_REGISTRY_SHARED_SECRET must be at least 32 characters")
    return value


def _key() -> bytes:
    raw = os.getenv("WORKFLOW_PROVIDER_REGISTRY_KEY", "")
    if len(raw) < 32:
        raise RuntimeError("WORKFLOW_PROVIDER_REGISTRY_KEY must be at least 32 characters")
    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        if len(decoded) == 32:
            return decoded
    except Exception:
        pass
    return hashlib.sha256(raw.encode()).digest()


def _seal(value: object, aad: bytes) -> str:
    nonce = os.urandom(12)
    data = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
    return base64.urlsafe_b64encode(nonce + AESGCM(_key()).encrypt(nonce, data, aad)).decode()


def _open(value: str, aad: bytes) -> object:
    raw = base64.urlsafe_b64decode(str(value).encode())
    if len(raw) < 29:
        raise ValueError("encrypted registry value is invalid")
    return json.loads(AESGCM(_key()).decrypt(raw[:12], raw[12:], aad).decode())


def _auth(token: str | None) -> None:
    if not token or not hmac.compare_digest(token, _secret()):
        raise HTTPException(status_code=401, detail="unauthorized")


@contextmanager
def _db():
    conn = psycopg.connect(
        host=os.getenv("WORKFLOW_DB_HOST", "workflow-postgres"),
        port=int(os.getenv("WORKFLOW_DB_PORT", "5432")),
        dbname=os.getenv("WORKFLOW_DB_NAME", "xvond_workflow"),
        user=os.getenv("WORKFLOW_DB_USER", "xvond_workflow"),
        password=os.environ["WORKFLOW_DB_PASSWORD"],
        connect_timeout=5,
        application_name="xvond-workflow-registry",
    )
    try:
        yield conn
    finally:
        conn.close()


class Provision(BaseModel):
    company_id: int = Field(gt=0)
    connection_key: str = Field(min_length=16, max_length=200)
    channel_id: int = Field(gt=0)
    agent_id: int = Field(gt=0)
    channel_type: str = Field(min_length=1, max_length=50)
    provider_type: str = Field(min_length=1, max_length=50)
    provider_url: str = Field(min_length=9, max_length=2000)
    provider_secret: str = Field(min_length=32, max_length=4000)
    provider_config: dict = Field(default_factory=dict)
    provider_account_label: str | None = Field(default=None, max_length=200)

    @field_validator("connection_key")
    @classmethod
    def normalize_connection_key(cls, value: str) -> str:
        clean = str(value or "").strip()
        if len(clean) < 16:
            raise ValueError("connection_key must be at least 16 characters")
        return clean

    @field_validator("channel_type", "provider_type")
    @classmethod
    def normalize_type(cls, value: str) -> str:
        return str(value or "").strip().lower()

    @field_validator("provider_url")
    @classmethod
    def validate_provider_url(cls, value: str) -> str:
        clean = str(value or "").strip()
        parsed = urlsplit(clean)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("provider_url must be an HTTPS URL without embedded credentials")
        host = parsed.hostname.rstrip(".").lower()
        if host == "localhost" or host.endswith(".localhost"):
            raise ValueError("provider_url cannot target localhost")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
        ):
            raise ValueError("provider_url cannot target a private or local address")
        return clean

    @model_validator(mode="after")
    def validate_provider_contract(self):
        allowed_channels = PROVIDER_CHANNELS.get(self.provider_type)
        if not allowed_channels or self.channel_type not in allowed_channels:
            raise ValueError("provider_type does not match channel_type")
        encoded = json.dumps(
            self.provider_config,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        if len(encoded) > MAX_PROVIDER_CONFIG_BYTES:
            raise ValueError("provider_config is too large")
        return self


def _aad(company_id: int, connection_key: str) -> bytes:
    return f"{company_id}:{connection_key}".encode()


@app.get("/health")
def health():
    _secret()
    _key()
    with _db() as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.xvond_managed_channel_routes')")
        row = cur.fetchone()
    if not row or row[0] is None:
        raise RuntimeError("workflow registry schema is not initialized")
    return {"status": "ok"}


@app.post("/v1/routes")
def provision(payload: Provision, x_xvond_registry_secret: str | None = Header(default=None)):
    _auth(x_xvond_registry_secret)
    aad = _aad(payload.company_id, payload.connection_key)
    secret_enc = _seal(payload.provider_secret, aad)
    config_enc = _seal(payload.provider_config, aad)
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO xvond_managed_channel_routes
                (company_id, connection_key, channel_id, agent_id, channel_type, provider_type,
                 provider_url, provider_secret_enc, provider_config_enc, provider_account_label, active)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE)
                ON CONFLICT (company_id, connection_key) DO UPDATE SET
                  provider_type=EXCLUDED.provider_type, provider_url=EXCLUDED.provider_url,
                  provider_secret_enc=EXCLUDED.provider_secret_enc,
                  provider_config_enc=EXCLUDED.provider_config_enc,
                  provider_account_label=EXCLUDED.provider_account_label,
                  active=TRUE, updated_at=NOW()
                WHERE xvond_managed_channel_routes.channel_id=EXCLUDED.channel_id
                  AND xvond_managed_channel_routes.agent_id=EXCLUDED.agent_id
                  AND xvond_managed_channel_routes.channel_type=EXCLUDED.channel_type
                RETURNING channel_id""",
                (
                    payload.company_id,
                    payload.connection_key,
                    payload.channel_id,
                    payload.agent_id,
                    payload.channel_type,
                    payload.provider_type,
                    payload.provider_url,
                    secret_enc,
                    config_enc,
                    payload.provider_account_label,
                ),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=409, detail="route_identity_conflict")
        conn.commit()
    return {"success": True, "channel_id": payload.channel_id}


@app.get("/v1/routes/{company_id}/{connection_key}")
def lookup(company_id: int, connection_key: str, x_xvond_registry_secret: str | None = Header(default=None)):
    _auth(x_xvond_registry_secret)
    clean_key = str(connection_key or "").strip()
    if len(clean_key) < 16 or len(clean_key) > 200:
        raise HTTPException(status_code=400, detail="invalid_connection_key")
    with _db() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT channel_id,agent_id,channel_type,provider_type,provider_url,
                      provider_secret_enc,provider_config_enc,provider_account_label
               FROM xvond_managed_channel_routes
               WHERE company_id=%s AND connection_key=%s AND active=TRUE
                 AND provider_secret_enc IS NOT NULL
                 AND provider_config_enc IS NOT NULL""",
            (company_id, clean_key),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="route_not_found")
    aad = _aad(company_id, clean_key)
    return {
        "success": True,
        "route": {
            "company_id": company_id,
            "connection_key": clean_key,
            "channel_id": row[0],
            "agent_id": row[1],
            "channel_type": row[2],
            "provider_type": row[3],
            "provider_url": row[4],
            "provider_secret": _open(row[5], aad),
            "provider_config": _open(row[6], aad),
            "provider_account_label": row[7],
        },
    }


@app.delete("/v1/routes/{company_id}/{connection_key}")
def deactivate(company_id: int, connection_key: str, x_xvond_registry_secret: str | None = Header(default=None)):
    _auth(x_xvond_registry_secret)
    clean_key = str(connection_key or "").strip()
    if len(clean_key) < 16 or len(clean_key) > 200:
        raise HTTPException(status_code=400, detail="invalid_connection_key")
    with _db() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE xvond_managed_channel_routes SET active=FALSE,updated_at=NOW() WHERE company_id=%s AND connection_key=%s",
            (company_id, clean_key),
        )
        changed = cur.rowcount
        conn.commit()
    return {"success": True, "deactivated": changed > 0}
