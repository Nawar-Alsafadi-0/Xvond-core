#!/usr/bin/env python3
"""Verify that Xvond Core is reachable through its canonical public HTTPS origin."""

from __future__ import annotations

import argparse
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _health_url(base_url: str) -> str:
    raw = str(base_url or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("PUBLIC_BASE_URL must be a valid HTTPS origin")
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(
            "PUBLIC_BASE_URL must be an HTTPS origin without credentials, path, query or fragment"
        )
    return raw.rstrip("/") + "/health/ready"


def probe_public_origin(base_url: str, *, timeout: float = 10.0, opener=None) -> dict:
    url = _health_url(base_url)
    client = opener or build_opener(_NoRedirect())
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Xvond-Production-Release/1.0",
        },
        method="GET",
    )
    try:
        with client.open(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200))
            body = response.read()
    except HTTPError as exc:
        raise RuntimeError(f"Public Core health returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"Public Core health is unreachable: {exc.reason}") from exc

    if status != 200:
        raise RuntimeError(f"Public Core health returned HTTP {status}")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Public Core health did not return valid JSON") from exc

    if payload.get("status") != "healthy":
        raise RuntimeError("Public Core health is not healthy")
    if str(payload.get("environment") or "").strip().lower() not in {"production", "prod"}:
        raise RuntimeError("Public Core health did not report production environment")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    try:
        payload = probe_public_origin(args.base_url, timeout=args.timeout)
    except (ValueError, RuntimeError) as exc:
        print(f"Public origin probe failed: {exc}")
        return 1

    print(
        "Public origin healthy: "
        f"{_health_url(args.base_url)} "
        f"(environment={payload.get('environment')}, version={payload.get('version')})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
