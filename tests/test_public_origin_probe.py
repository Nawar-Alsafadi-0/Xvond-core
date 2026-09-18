import io
import json

import pytest

from scripts.public_origin_probe import _health_url, probe_public_origin


class FakeResponse:
    def __init__(self, payload, status=200):
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._body


class FakeOpener:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def open(self, request, timeout=10.0):
        self.requests.append((request, timeout))
        return self.response


def test_health_url_requires_clean_https_origin():
    assert _health_url("https://api.xvond.com") == "https://api.xvond.com/health/ready"
    assert _health_url("https://api.xvond.com/") == "https://api.xvond.com/health/ready"

    for value in (
        "http://api.xvond.com",
        "https://api.xvond.com/path",
        "https://api.xvond.com?x=1",
        "https://user:pass@api.xvond.com",
    ):
        with pytest.raises(ValueError):
            _health_url(value)


def test_public_origin_probe_requires_healthy_production_json():
    opener = FakeOpener(
        FakeResponse(
            {
                "status": "healthy",
                "database": "ok",
                "redis": "ok",
                "environment": "production",
                "version": "1.0.0",
            }
        )
    )

    payload = probe_public_origin(
        "https://api.xvond.com",
        timeout=7,
        opener=opener,
    )

    assert payload["status"] == "healthy"
    request, timeout = opener.requests[0]
    assert request.full_url == "https://api.xvond.com/health/ready"
    assert request.get_header("Accept") == "application/json"
    assert timeout == 7


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "unhealthy", "environment": "production"},
        {"status": "healthy", "environment": "development"},
        {"status": "healthy", "environment": ""},
    ],
)
def test_public_origin_probe_fails_closed_on_wrong_runtime(payload):
    opener = FakeOpener(FakeResponse(payload))
    with pytest.raises(RuntimeError):
        probe_public_origin("https://api.xvond.com", opener=opener)
