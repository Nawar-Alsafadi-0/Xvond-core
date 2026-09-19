import pytest

from backend.app.modules.integrations.openapi_contract import (
    normalize_openapi_document,
    parse_openapi_text,
)


def _document():
    return {
        "openapi": "3.1.0",
        "info": {"title": "Vendor API"},
        "servers": [{"url": "https://api.vendor.example/v1"}],
        "paths": {
            "/orders/{order_id}": {
                "get": {
                    "operationId": "getOrder",
                    "summary": "Fetch one order",
                    "parameters": [
                        {"name": "order_id", "in": "path", "required": True},
                        {"name": "expand", "in": "query"},
                    ],
                },
                "delete": {
                    "operationId": "cancelOrder",
                    "parameters": [
                        {"name": "order_id", "in": "path", "required": True},
                    ],
                },
            },
            "/orders": {
                "post": {
                    "operationId": "createOrder",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    },
                }
            },
        },
    }


def test_openapi_document_becomes_bounded_executable_operations():
    contract = normalize_openapi_document(_document())

    assert contract["base_url"] == "https://api.vendor.example/v1"
    assert set(contract["operations"]) == {"get_order", "cancel_order", "create_order"}

    lookup = contract["operations"]["get_order"]
    assert lookup["method"] == "GET"
    assert lookup["endpoint"] == "/orders/{order_id}"
    assert lookup["input_mode"] == "query"
    assert lookup["path_params"] == ["order_id"]

    create = contract["operations"]["create_order"]
    assert create["method"] == "POST"
    assert create["input_mode"] == "json"


def test_openapi_yaml_is_supported():
    contract = parse_openapi_text(
        """
openapi: 3.0.3
info:
  title: Search API
paths:
  /search:
    get:
      operationId: searchItems
      parameters:
        - name: q
          in: query
"""
    )
    assert contract["operations"]["search_items"]["method"] == "GET"
    assert contract["operations"]["search_items"]["input_mode"] == "query"


def test_openapi_rejects_non_contract_and_unsafe_path_parameters():
    with pytest.raises(ValueError):
        normalize_openapi_document({"paths": {}})

    contract = normalize_openapi_document(
        {
            "openapi": "3.0.0",
            "paths": {
                "/unsafe/{bad-name}": {
                    "get": {"operationId": "unsafe"},
                },
                "https://evil.example/x": {
                    "get": {"operationId": "absolute"},
                },
                "/safe": {
                    "trace": {"operationId": "trace"},
                    "get": {"operationId": "safe"},
                },
            },
        }
    )
    assert set(contract["operations"]) == {"safe"}


def test_openapi_ignores_dynamic_server_templates():
    document = _document()
    document["servers"] = [{"url": "https://{tenant}.example.com/v1"}]
    assert normalize_openapi_document(document)["base_url"] is None
