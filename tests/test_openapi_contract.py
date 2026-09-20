import pytest

from backend.app.modules.integrations.openapi_contract import (
    discover_openapi_contract,
    normalize_openapi_document,
    openapi_discovery_urls,
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



def test_openapi_document_tracks_required_query_parameters():
    contract = normalize_openapi_document({
        "openapi": "3.0.3",
        "paths": {
            "/search": {
                "get": {
                    "operationId": "searchItems",
                    "parameters": [
                        {"name": "q", "in": "query", "required": True},
                        {"name": "page", "in": "query", "required": False},
                    ],
                }
            }
        },
    })
    assert contract["operations"]["search_items"]["required_query_params"] == ["q"]


def test_openapi_discovery_stays_on_configured_host_and_finds_standard_path(monkeypatch):
    calls = []

    def fake_http(**kwargs):
        calls.append(kwargs["url"])
        if kwargs["url"].endswith("/swagger.json"):
            return {
                "status_code": 200,
                "truncated": False,
                "response": """
                {
                  "openapi": "3.0.3",
                  "info": {"title": "Discovered API"},
                  "servers": [{"url": "https://other.example/v1"}],
                  "paths": {"/health": {"get": {"operationId": "health"}}}
                }
                """,
            }
        return {"status_code": 404, "truncated": False, "response": "not found"}

    monkeypatch.setattr(
        "backend.app.modules.integrations.openapi_contract.safe_http_request",
        fake_http,
    )

    contract = discover_openapi_contract("https://api.vendor.example/v1")
    assert contract["discovery_url"] == "https://api.vendor.example/v1/swagger.json"
    assert contract["base_url"] is None
    assert set(contract["operations"]) == {"health"}
    assert calls[:2] == [
        "https://api.vendor.example/v1/openapi.json",
        "https://api.vendor.example/v1/swagger.json",
    ]


def test_openapi_discovery_candidates_are_bounded_to_same_https_origin():
    urls = openapi_discovery_urls("https://api.vendor.example/v1")
    assert len(urls) <= 10
    assert urls[0] == "https://api.vendor.example/v1/openapi.json"
    assert all(url.startswith("https://api.vendor.example/") for url in urls)

    with pytest.raises(ValueError):
        openapi_discovery_urls("http://api.vendor.example")


def test_openapi_discovery_fails_closed_when_no_contract_is_found(monkeypatch):
    monkeypatch.setattr(
        "backend.app.modules.integrations.openapi_contract.safe_http_request",
        lambda **kwargs: {
            "status_code": 404,
            "truncated": False,
            "response": "missing",
        },
    )
    with pytest.raises(ValueError, match="No OpenAPI/Swagger"):
        discover_openapi_contract("https://api.vendor.example")



def test_openapi_discovery_rejects_cross_port_execution_base(monkeypatch):
    def fake_http(**kwargs):
        return {
            "status_code": 200,
            "truncated": False,
            "response": """
            {
              "openapi": "3.0.3",
              "servers": [{"url": "https://api.vendor.example:444/v1"}],
              "paths": {"/health": {"get": {"operationId": "health"}}}
            }
            """,
        }

    monkeypatch.setattr(
        "backend.app.modules.integrations.openapi_contract.safe_http_request",
        fake_http,
    )
    contract = discover_openapi_contract("https://api.vendor.example/v1")
    assert contract["base_url"] is None


def test_openapi_extracts_generic_auth_schemes():
    document = {
        "openapi": "3.0.3",
        "info": {"title": "Protected API"},
        "servers": [{"url": "https://api.example.com"}],
        "components": {
            "securitySchemes": {
                "BearerAuth": {"type": "http", "scheme": "bearer"},
                "PartnerKey": {"type": "apiKey", "in": "header", "name": "X-Partner-Key"},
                "BasicAuth": {"type": "http", "scheme": "basic"},
            }
        },
        "paths": {
            "/me": {
                "get": {
                    "operationId": "getMe",
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    contract = normalize_openapi_document(document)
    assert {"name": "BearerAuth", "auth_type": "bearer"} in contract["auth_schemes"]
    assert {
        "name": "PartnerKey",
        "auth_type": "api_key_header",
        "api_key_name": "X-Partner-Key",
    } in contract["auth_schemes"]
    assert {"name": "BasicAuth", "auth_type": "basic"} in contract["auth_schemes"]


def test_openapi_preserves_safe_oauth_authorization_contract():
    document = {
        "openapi": "3.0.3",
        "info": {"title": "OAuth API"},
        "servers": [{"url": "https://api.example.com"}],
        "components": {
            "securitySchemes": {
                "OAuth": {
                    "type": "oauth2",
                    "flows": {
                        "authorizationCode": {
                            "authorizationUrl": "https://accounts.example.com/oauth/authorize",
                            "tokenUrl": "https://accounts.example.com/oauth/token",
                            "scopes": {"orders.read": "Read orders"},
                        }
                    },
                }
            }
        },
        "paths": {"/orders": {"get": {"operationId": "listOrders"}}},
    }
    contract = normalize_openapi_document(document)
    assert contract["auth_schemes"] == [{
        "name": "OAuth",
        "auth_type": "oauth",
        "flows": [{
            "flow": "authorization_code",
            "authorization_url": "https://accounts.example.com/oauth/authorize",
            "token_url": "https://accounts.example.com/oauth/token",
            "scopes": ["orders.read"],
        }],
    }]


def test_openapi_drops_unsafe_oauth_urls():
    document = {
        "openapi": "3.0.3",
        "info": {"title": "Unsafe OAuth API"},
        "servers": [{"url": "https://api.example.com"}],
        "components": {
            "securitySchemes": {
                "OAuth": {
                    "type": "oauth2",
                    "flows": {
                        "authorizationCode": {
                            "authorizationUrl": "http://accounts.example.com/oauth/authorize",
                            "tokenUrl": "https://accounts.example.com/oauth/token",
                            "scopes": {},
                        }
                    },
                }
            }
        },
        "paths": {"/orders": {"get": {"operationId": "listOrders"}}},
    }
    assert normalize_openapi_document(document)["auth_schemes"] == []

def test_openapi_extracts_required_json_body_fields_through_refs_and_allof():
    document = {
        "openapi": "3.0.3",
        "components": {
            "schemas": {
                "Customer": {
                    "type": "object",
                    "required": ["customer_name"],
                    "properties": {
                        "customer_name": {
                            "type": "string",
                            "description": "Customer full name",
                        }
                    },
                },
                "BookingRequest": {
                    "allOf": [
                        {"$ref": "#/components/schemas/Customer"},
                        {
                            "type": "object",
                            "required": ["service_id", "date"],
                            "properties": {
                                "service_id": {"type": "integer"},
                                "date": {"type": "string", "format": "date"},
                                "notes": {"type": "string"},
                            },
                        },
                    ]
                },
            }
        },
        "paths": {
            "/appointments": {
                "post": {
                    "operationId": "createAppointment",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/BookingRequest"}
                            }
                        },
                    },
                }
            }
        },
    }

    operation = normalize_openapi_document(document)["operations"]["create_appointment"]

    assert operation["required_json_fields"] == [
        "customer_name",
        "service_id",
        "date",
    ]
    fields = {item["key"]: item for item in operation["json_fields"]}
    assert fields["customer_name"]["required"] is True
    assert fields["customer_name"]["description"] == "Customer full name"
    assert fields["service_id"]["type"] == "integer"
    assert fields["date"]["format"] == "date"
    assert fields["notes"]["required"] is False


def test_swagger_body_parameter_exposes_required_json_fields():
    document = {
        "swagger": "2.0",
        "paths": {
            "/orders": {
                "post": {
                    "operationId": "createOrder",
                    "parameters": [
                        {
                            "name": "body",
                            "in": "body",
                            "required": True,
                            "schema": {
                                "type": "object",
                                "required": ["sku", "quantity"],
                                "properties": {
                                    "sku": {"type": "string"},
                                    "quantity": {"type": "integer"},
                                },
                            },
                        }
                    ],
                }
            }
        },
    }

    operation = normalize_openapi_document(document)["operations"]["create_order"]
    assert operation["input_mode"] == "json"
    assert operation["required_json_fields"] == ["sku", "quantity"]

def test_openapi_tracks_all_declared_query_parameters_for_request_shaping():
    contract = normalize_openapi_document(
        {
            "openapi": "3.0.3",
            "paths": {
                "/search": {
                    "get": {
                        "operationId": "searchItems",
                        "parameters": [
                            {"name": "status", "in": "query", "required": True},
                            {"name": "limit", "in": "query", "required": False},
                            {"name": "ignored body", "in": "query", "required": False},
                        ],
                    }
                }
            },
        }
    )

    operation = contract["operations"]["search_items"]
    assert operation["query_params"] == ["status", "limit"]
    assert operation["required_query_params"] == ["status"]

def test_openapi_extracts_bounded_success_object_response_contract():
    contract = normalize_openapi_document(
        {
            "openapi": "3.0.3",
            "components": {
                "schemas": {
                    "CreatedOrder": {
                        "type": "object",
                        "required": ["id"],
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "Created order identifier",
                            },
                            "total": {"type": "number"},
                        },
                    }
                }
            },
            "paths": {
                "/orders": {
                    "post": {
                        "operationId": "createOrder",
                        "responses": {
                            "400": {"description": "bad request"},
                            "201": {
                                "description": "created",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/CreatedOrder"
                                        }
                                    }
                                },
                            },
                        },
                    }
                }
            },
        }
    )

    operation = contract["operations"]["create_order"]
    assert operation["response_status"] == "201"
    assert operation["response_kind"] == "object"
    fields = {item["key"]: item for item in operation["response_fields"]}
    assert fields["id"]["required"] is True
    assert fields["id"]["description"] == "Created order identifier"
    assert fields["total"]["type"] == "number"


def test_openapi_extracts_array_item_response_contract():
    contract = normalize_openapi_document(
        {
            "openapi": "3.0.3",
            "components": {
                "schemas": {
                    "Order": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "state": {"type": "string"},
                        },
                    }
                }
            },
            "paths": {
                "/orders": {
                    "get": {
                        "operationId": "listOrders",
                        "responses": {
                            "200": {
                                "description": "ok",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "array",
                                            "items": {
                                                "$ref": "#/components/schemas/Order"
                                            },
                                        }
                                    }
                                },
                            }
                        },
                    }
                }
            },
        }
    )

    operation = contract["operations"]["list_orders"]
    assert operation["response_status"] == "200"
    assert operation["response_kind"] == "array"
    assert operation["response_item_kind"] == "object"
    assert [item["key"] for item in operation["response_item_fields"]] == [
        "id",
        "state",
    ]


def test_swagger_success_response_schema_is_supported():
    contract = normalize_openapi_document(
        {
            "swagger": "2.0",
            "paths": {
                "/record": {
                    "post": {
                        "operationId": "createRecord",
                        "responses": {
                            "201": {
                                "description": "created",
                                "schema": {
                                    "type": "object",
                                    "required": ["id"],
                                    "properties": {"id": {"type": "integer"}},
                                },
                            }
                        },
                    }
                }
            },
        }
    )

    operation = contract["operations"]["create_record"]
    assert operation["response_status"] == "201"
    assert operation["response_kind"] == "object"
    assert operation["response_fields"][0]["key"] == "id"
    assert operation["response_fields"][0]["type"] == "integer"

def test_openapi_urlencoded_request_body_becomes_form_contract():
    contract = normalize_openapi_document(
        {
            "openapi": "3.0.3",
            "paths": {
                "/token": {
                    "post": {
                        "operationId": "createToken",
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/x-www-form-urlencoded": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["client_id", "grant_type"],
                                        "properties": {
                                            "client_id": {"type": "string"},
                                            "grant_type": {
                                                "type": "string",
                                                "enum": ["client_credentials"],
                                            },
                                            "scope": {"type": "string"},
                                        },
                                    }
                                }
                            },
                        },
                    }
                }
            },
        }
    )

    operation = contract["operations"]["create_token"]
    assert operation["input_mode"] == "form"
    assert operation["required_form_fields"] == ["client_id", "grant_type"]
    fields = {item["key"]: item for item in operation["form_fields"]}
    assert fields["client_id"]["required"] is True
    assert fields["grant_type"]["enum"] == ["client_credentials"]
    assert fields["scope"]["required"] is False
    assert operation["required_json_fields"] == []
    assert operation["json_fields"] == []


def test_openapi_binary_multipart_uses_owned_file_contract():
    contract = normalize_openapi_document(
        {
            "openapi": "3.0.3",
            "paths": {
                "/health": {
                    "get": {"operationId": "health"}
                },
                "/upload": {
                    "post": {
                        "operationId": "uploadFile",
                        "requestBody": {
                            "required": True,
                            "content": {
                                "multipart/form-data": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["file"],
                                        "properties": {
                                            "file": {
                                                "type": "string",
                                                "format": "binary",
                                            }
                                        },
                                    }
                                }
                            },
                        },
                    }
                },
            },
        }
    )

    assert set(contract["operations"]) == {"health", "upload_file"}
    operation = contract["operations"]["upload_file"]
    assert operation["input_mode"] == "multipart"
    assert operation["required_form_fields"] == ["file"]
    assert operation["form_fields"] == [
        {
            "key": "file",
            "required": True,
            "type": "string",
            "format": "binary",
        }
    ]


def test_swagger_formdata_urlencoded_is_supported_and_binary_multipart_is_not():
    urlencoded = normalize_openapi_document(
        {
            "swagger": "2.0",
            "consumes": ["application/x-www-form-urlencoded"],
            "paths": {
                "/session": {
                    "post": {
                        "operationId": "createSession",
                        "parameters": [
                            {
                                "name": "username",
                                "in": "formData",
                                "required": True,
                                "type": "string",
                            },
                            {
                                "name": "remember",
                                "in": "formData",
                                "required": False,
                                "type": "boolean",
                            },
                        ],
                    }
                }
            },
        }
    )
    operation = urlencoded["operations"]["create_session"]
    assert operation["input_mode"] == "form"
    assert operation["required_form_fields"] == ["username"]
    assert [item["key"] for item in operation["form_fields"]] == [
        "username",
        "remember",
    ]

    multipart = normalize_openapi_document(
        {
            "swagger": "2.0",
            "consumes": ["multipart/form-data"],
            "paths": {
                "/health": {"get": {"operationId": "health"}},
                "/upload": {
                    "post": {
                        "operationId": "upload",
                        "parameters": [
                            {
                                "name": "file",
                                "in": "formData",
                                "required": True,
                                "type": "file",
                            }
                        ],
                    }
                },
            },
        }
    )
    assert set(multipart["operations"]) == {"health", "upload"}
    upload = multipart["operations"]["upload"]
    assert upload["input_mode"] == "multipart"
    assert upload["form_fields"][0]["format"] == "binary"

def test_openapi_root_array_json_request_becomes_bounded_array_contract():
    contract = normalize_openapi_document(
        {
            "openapi": "3.0.3",
            "paths": {
                "/batch": {
                    "post": {
                        "operationId": "batchCreate",
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "required": ["sku"],
                                            "properties": {
                                                "sku": {"type": "string"},
                                                "quantity": {"type": "integer"},
                                            },
                                        },
                                    }
                                }
                            },
                        },
                    }
                },
            },
        }
    )

    operation = contract["operations"]["batch_create"]
    assert operation["input_mode"] == "json_array"
    assert operation["array_item_kind"] == "object"
    assert operation["array_max_items"] == 100
    assert operation["required_array_item_fields"] == ["sku"]
    fields = {item["key"]: item for item in operation["array_item_fields"]}
    assert fields["sku"]["required"] is True
    assert fields["quantity"]["type"] == "integer"

def test_openapi_scalar_multipart_request_is_supported_without_binary_fields():
    contract = normalize_openapi_document(
        {
            "openapi": "3.0.3",
            "paths": {
                "/submit": {
                    "post": {
                        "operationId": "submitForm",
                        "requestBody": {
                            "required": True,
                            "content": {
                                "multipart/form-data": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["title", "count"],
                                        "properties": {
                                            "title": {"type": "string"},
                                            "count": {"type": "integer"},
                                            "published": {"type": "boolean"},
                                        },
                                    }
                                }
                            },
                        },
                    }
                }
            },
        }
    )

    operation = contract["operations"]["submit_form"]
    assert operation["input_mode"] == "multipart"
    assert operation["required_form_fields"] == ["title", "count"]
    assert [item["key"] for item in operation["form_fields"]] == [
        "title",
        "count",
        "published",
    ]


def test_swagger_scalar_multipart_formdata_is_supported():
    contract = normalize_openapi_document(
        {
            "swagger": "2.0",
            "consumes": ["multipart/form-data"],
            "paths": {
                "/submit": {
                    "post": {
                        "operationId": "submitForm",
                        "parameters": [
                            {
                                "name": "title",
                                "in": "formData",
                                "required": True,
                                "type": "string",
                            },
                            {
                                "name": "count",
                                "in": "formData",
                                "required": False,
                                "type": "integer",
                            },
                        ],
                    }
                }
            },
        }
    )

    operation = contract["operations"]["submit_form"]
    assert operation["input_mode"] == "multipart"
    assert operation["required_form_fields"] == ["title"]
    assert [item["key"] for item in operation["form_fields"]] == ["title", "count"]

def test_openapi_root_scalar_array_is_supported_but_nested_arrays_are_not():
    strings = normalize_openapi_document(
        {
            "openapi": "3.0.3",
            "paths": {
                "/tags": {
                    "post": {
                        "operationId": "replaceTags",
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    }
                                }
                            },
                        },
                    }
                }
            },
        }
    )
    assert strings["operations"]["replace_tags"]["array_item_kind"] == "string"

    nested = normalize_openapi_document(
        {
            "openapi": "3.0.3",
            "paths": {
                "/health": {"get": {"operationId": "health"}},
                "/matrix": {
                    "post": {
                        "operationId": "matrix",
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "array",
                                        "items": {
                                            "type": "array",
                                            "items": {"type": "number"},
                                        },
                                    }
                                }
                            },
                        },
                    }
                },
            },
        }
    )
    assert set(nested["operations"]) == {"health"}

def test_openapi_nested_json_request_retains_recursive_schema():
    contract = normalize_openapi_document(
        {
            "openapi": "3.0.3",
            "paths": {
                "/orders": {
                    "post": {
                        "operationId": "createOrder",
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["customer", "items"],
                                        "properties": {
                                            "customer": {
                                                "type": "object",
                                                "required": ["name"],
                                                "properties": {
                                                    "name": {"type": "string"},
                                                    "phone": {"type": "string"},
                                                },
                                            },
                                            "items": {
                                                "type": "array",
                                                "items": {
                                                    "type": "object",
                                                    "required": ["sku"],
                                                    "properties": {
                                                        "sku": {"type": "string"},
                                                        "quantity": {"type": "integer"},
                                                    },
                                                },
                                            },
                                        },
                                    }
                                }
                            },
                        },
                    }
                }
            },
        }
    )

    fields = {
        item["key"]: item
        for item in contract["operations"]["create_order"]["json_fields"]
    }
    assert fields["customer"]["schema"] == {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "phone": {"type": "string"},
        },
        "required": ["name"],
    }
    assert fields["items"]["schema"] == {
        "type": "array",
        "max_items": 100,
        "items": {
            "type": "object",
            "properties": {
                "sku": {"type": "string"},
                "quantity": {"type": "integer"},
            },
            "required": ["sku"],
        },
    }

def test_openapi_imports_safe_header_parameters_and_drops_reserved_headers():
    contract = normalize_openapi_document({
        "openapi": "3.0.3",
        "paths": {"/records": {
            "parameters": [{"name": "X-Workspace-ID", "in": "header", "required": True, "schema": {"type": "string"}}],
            "post": {
                "operationId": "createRecord",
                "parameters": [
                    {"name": "X-Region", "in": "header", "required": False, "schema": {"type": "string"}},
                    {"name": "Authorization", "in": "header", "required": True, "schema": {"type": "string"}},
                    {"name": "Content-Type", "in": "header", "required": True, "schema": {"type": "string"}},
                    {"name": "Idempotency-Key", "in": "header", "required": True, "schema": {"type": "string"}},
                ],
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}
                }}}},
            },
        }},
    })
    operation = contract["operations"]["create_record"]
    assert operation["header_params"] == ["X-Workspace-ID", "X-Region"]
    assert operation["required_header_params"] == ["X-Workspace-ID"]
