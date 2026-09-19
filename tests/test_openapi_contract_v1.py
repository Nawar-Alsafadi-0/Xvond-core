import pytest

from backend.app.modules.integrations.openapi_contract import (
    normalize_openapi_document,
    parse_openapi_text,
)


def test_openapi_contract_builds_generic_named_operations():
    contract = normalize_openapi_document({
        "openapi": "3.0.3",
        "info": {"title": "Unknown Vendor API"},
        "servers": [{"url": "https://api.vendor.example/v1"}],
        "paths": {
            "/prices": {"get": {
                "operationId": "listPrices",
                "summary": "List current prices",
                "parameters": [{"name": "sku", "in": "query", "schema": {"type": "string"}}],
            }},
            "/orders/{order_id}": {"patch": {
                "operationId": "updateOrder",
                "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}},
            }},
        },
    })
    assert contract["base_url"] == "https://api.vendor.example/v1"
    assert contract["operations"]["listprices"]["method"] == "GET"
    assert contract["operations"]["listprices"]["input_mode"] == "query"
    assert contract["operations"]["updateorder"]["method"] == "PATCH"
    assert contract["operations"]["updateorder"]["path_params"] == ["order_id"]


def test_openapi_yaml_is_supported_without_provider_specific_code():
    contract = parse_openapi_text("""
openapi: 3.0.0
info:
  title: Generic Service
paths:
  /records:
    post:
      operationId: create_record
      requestBody:
        content:
          application/json:
            schema:
              type: object
""")
    assert contract["operations"]["create_record"]["endpoint"] == "/records"
    assert contract["operations"]["create_record"]["input_mode"] == "json"


def test_openapi_ignores_unsafe_servers_and_unsupported_methods():
    contract = normalize_openapi_document({
        "openapi": "3.0.0",
        "info": {"title": "Unsafe hints"},
        "servers": [
            {"url": "http://127.0.0.1:9000"},
            {"url": "https://{tenant}.example.com"},
        ],
        "paths": {
            "/ok": {"get": {"operationId": "ok"}},
            "/trace": {"trace": {"operationId": "trace_me"}},
        },
    })
    assert contract["base_url"] is None
    assert set(contract["operations"]) == {"ok"}


def test_openapi_rejects_documents_without_executable_paths():
    with pytest.raises(ValueError):
        normalize_openapi_document({
            "openapi": "3.0.0",
            "info": {"title": "No operations"},
            "paths": {"/trace": {"trace": {"operationId": "trace_me"}}},
        })
