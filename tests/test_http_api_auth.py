import base64

import pytest

from backend.app.modules.integrations.catalog import validate_integration_config
from backend.app.modules.integrations.http_api_auth import (
    apply_http_api_auth,
    normalized_http_api_auth_type,
    validate_http_api_auth_config,
)


def test_legacy_api_key_keeps_bearer_behavior():
    config = {"api_key": "secret-token"}
    assert normalized_http_api_auth_type(config) == "bearer"
    url, headers = apply_http_api_auth(
        url="https://api.example.com/v1/items",
        headers={"Accept": "application/json"},
        config=config,
    )
    assert url == "https://api.example.com/v1/items"
    assert headers["Authorization"] == "Bearer secret-token"


def test_custom_api_header_auth_supports_bounded_name_and_prefix():
    config = {
        "auth_type": "api_key_header",
        "api_key": "secret-token",
        "api_key_name": "X-Vendor-Key",
        "api_key_prefix": "Token",
    }
    url, headers = apply_http_api_auth(
        url="https://api.example.com/v1/items",
        headers={},
        config=config,
    )
    assert url == "https://api.example.com/v1/items"
    assert headers["X-Vendor-Key"] == "Token secret-token"


def test_custom_api_query_auth_appends_secret_parameter():
    url, headers = apply_http_api_auth(
        url="https://api.example.com/v1/items?q=red",
        headers={},
        config={
            "auth_type": "api_key_query",
            "api_key": "secret token",
            "api_key_name": "key",
        },
    )
    assert url == "https://api.example.com/v1/items?q=red&key=secret+token"
    assert headers == {}


def test_custom_api_basic_auth_is_encoded():
    url, headers = apply_http_api_auth(
        url="https://api.example.com/v1/items",
        headers={},
        config={
            "auth_type": "basic",
            "username": "alice",
            "password": "p@ss",
        },
    )
    expected = base64.b64encode(b"alice:p@ss").decode("ascii")
    assert url == "https://api.example.com/v1/items"
    assert headers["Authorization"] == f"Basic {expected}"


@pytest.mark.parametrize(
    "config",
    [
        {"auth_type": "bearer"},
        {"auth_type": "api_key_header", "api_key": "x", "api_key_name": "Host"},
        {"auth_type": "api_key_query", "api_key": "x", "api_key_name": "bad name"},
        {"auth_type": "basic", "username": "alice"},
        {"auth_type": "unknown"},
    ],
)
def test_invalid_generic_api_auth_fails_closed(config):
    with pytest.raises(ValueError):
        validate_http_api_auth_config(config)


def test_custom_api_catalog_enforces_conditional_auth_contract():
    assert validate_integration_config(
        "custom_api",
        {
            "base_url": "https://api.example.com",
            "validation_endpoint": "/health",
            "auth_type": "api_key_header",
            "api_key": "secret",
            "api_key_name": "X-API-Key",
        },
    ) is True

    with pytest.raises(ValueError):
        validate_integration_config(
            "custom_api",
            {
                "base_url": "https://api.example.com",
                "validation_endpoint": "/health",
                "auth_type": "basic",
                "username": "alice",
            },
        )



def test_custom_api_can_be_created_before_validation_endpoint_is_known():
    assert validate_integration_config(
        "custom_api",
        {
            "base_url": "https://api.example.com",
            "auth_type": "none",
        },
    ) is True
