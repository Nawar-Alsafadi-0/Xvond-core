from backend.app.modules.integrations import capability_discovery as discovery


def test_grounded_docs_url_resolves_without_search(monkeypatch):
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return {
            "title": "Acme API",
            "base_url": "https://api.acme.example",
            "operations": {
                "create_order": {
                    "method": "POST",
                    "endpoint": "/orders",
                    "input_mode": "json",
                }
            },
        }

    monkeypatch.setattr(discovery, "fetch_openapi_contract", fake_fetch)
    monkeypatch.setattr(
        discovery,
        "_search_result_urls",
        lambda query: (_ for _ in ()).throw(AssertionError("search should not run")),
    )

    result = discovery.discover_openapi_contract(
        {
            "needed": True,
            "docs_url": "https://docs.acme.example/openapi.json",
            "search_queries": ["Acme API docs"],
        }
    )

    assert result["status"] == "resolved"
    assert result["source"] == "grounded_docs_url"
    assert calls == ["https://docs.acme.example/openapi.json"]


def test_public_search_discovers_openapi_candidate(monkeypatch):
    monkeypatch.setattr(
        discovery,
        "_search_result_urls",
        lambda query: ["https://docs.vendor.example/api"],
    )
    monkeypatch.setattr(
        discovery,
        "_openapi_links_from_page",
        lambda url: ["https://docs.vendor.example/openapi.json"],
    )
    monkeypatch.setattr(
        discovery,
        "fetch_openapi_contract",
        lambda url: {
            "title": "Vendor API",
            "base_url": "https://api.vendor.example",
            "operations": {
                "lookup": {
                    "method": "GET",
                    "endpoint": "/items",
                    "input_mode": "query",
                }
            },
        },
    )

    result = discovery.discover_openapi_contract(
        {
            "needed": True,
            "service_hint": "Vendor",
            "capability": "Look up inventory",
            "search_queries": ["Vendor inventory OpenAPI"],
        }
    )

    assert result["status"] == "resolved"
    assert result["source"] == "public_docs_search"
    assert result["docs_url"] == "https://docs.vendor.example/openapi.json"


def test_discovery_not_found_is_fail_closed(monkeypatch):
    monkeypatch.setattr(discovery, "_search_result_urls", lambda query: [])
    result = discovery.discover_openapi_contract(
        {
            "needed": True,
            "capability": "Unknown vendor action",
            "search_queries": ["Unknown vendor OpenAPI"],
        }
    )
    assert result["status"] == "not_found"
    assert result["contract"] is None


def test_public_api_probe_uses_only_safe_read_operation(monkeypatch):
    captured = []

    def fake_http(**kwargs):
        captured.append(kwargs)
        return {"status_code": 200, "response": "{}", "truncated": False}

    monkeypatch.setattr(discovery, "safe_http_request", fake_http)

    evidence = discovery.public_api_probe(
        {
            "base_url": "https://api.example.com",
            "operations": {
                "write": {
                    "method": "POST",
                    "endpoint": "/orders",
                    "path_params": [],
                    "required_query_params": [],
                },
                "needs_input": {
                    "method": "GET",
                    "endpoint": "/search",
                    "path_params": [],
                    "required_query_params": ["q"],
                },
                "health": {
                    "method": "GET",
                    "endpoint": "/health",
                    "path_params": [],
                    "required_query_params": [],
                },
            },
        }
    )

    assert evidence["validated"] is True
    assert evidence["operation"] == "health"
    assert captured[0]["method"] == "GET"
    assert captured[0]["url"] == "https://api.example.com/health"


def test_public_api_probe_refuses_write_only_contract(monkeypatch):
    monkeypatch.setattr(
        discovery,
        "safe_http_request",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not call write endpoint")),
    )
    assert discovery.public_api_probe(
        {
            "base_url": "https://api.example.com",
            "operations": {
                "create": {
                    "method": "POST",
                    "endpoint": "/orders",
                    "path_params": [],
                    "required_query_params": [],
                }
            },
        }
    ) is None


def test_authenticated_api_probe_applies_bearer_without_writes(monkeypatch):
    captured = []

    def fake_http(**kwargs):
        captured.append(kwargs)
        return {"status_code": 200, "response": "{}", "truncated": False}

    monkeypatch.setattr(discovery, "safe_http_request", fake_http)
    evidence = discovery.api_connection_probe(
        {
            "base_url": "https://api.example.com",
            "operations": {
                "create": {
                    "method": "POST",
                    "endpoint": "/orders",
                    "path_params": [],
                    "required_query_params": [],
                },
                "me": {
                    "method": "GET",
                    "endpoint": "/me",
                    "path_params": [],
                    "required_query_params": [],
                },
            },
        },
        auth_config={"auth_type": "bearer", "api_key": "secret-token"},
    )
    assert evidence["validated"] is True
    assert captured[0]["method"] == "GET"
    assert captured[0]["url"] == "https://api.example.com/me"
    assert captured[0]["headers"]["Authorization"] == "Bearer secret-token"
