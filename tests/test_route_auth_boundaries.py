from fastapi.routing import APIRoute, APIRouter

import backend.app.main as main_module


def _mounted_api_routes() -> list[APIRoute]:
    routes = []
    seen = set()
    for name, router in vars(main_module).items():
        if not name.endswith("_router") or not isinstance(router, APIRouter):
            continue
        if id(router) in seen:
            continue
        seen.add(id(router))
        routes.extend(route for route in router.routes if isinstance(route, APIRoute))
    return routes


def _dependency_names(route: APIRoute) -> set[str]:
    names = set()

    def walk(dependant):
        for child in getattr(dependant, "dependencies", []) or []:
            call = getattr(child, "call", None)
            name = getattr(call, "__name__", None)
            if name:
                names.add(name)
            walk(child)

    walk(route.dependant)
    return names


def test_every_admin_route_requires_authenticated_xvond_access():
    failures = []
    accepted = {
        "require_xvond_operator",
        "require_xvond_admin",
        "require_super_admin",
    }
    for route in _mounted_api_routes():
        if not route.path.startswith("/admin/") and route.path != "/admin":
            continue
        names = _dependency_names(route)
        if not accepted.intersection(names):
            failures.append((sorted(route.methods), route.path, sorted(names)))
    assert failures == []


def test_mutating_admin_routes_never_rely_on_read_only_operator_access_only():
    failures = []
    mutation_methods = {"POST", "PUT", "PATCH", "DELETE"}
    for route in _mounted_api_routes():
        if not route.path.startswith("/admin/") and route.path != "/admin":
            continue
        if not mutation_methods.intersection(route.methods or set()):
            continue
        names = _dependency_names(route)
        if "require_xvond_operator" in names and not {
            "require_xvond_admin",
            "require_super_admin",
        }.intersection(names):
            failures.append((sorted(route.methods), route.path, sorted(names)))
    assert failures == []


def test_every_customer_namespace_route_requires_customer_authentication():
    failures = []
    accepted = {
        "require_customer_user",
        "require_customer_operator",
        "require_customer_manager",
        "require_customer_admin",
    }
    public_oauth_callbacks = {
        "/customer/meta/channels/instagram/oauth/callback",
    }
    for route in _mounted_api_routes():
        if not route.path.startswith("/customer/") and route.path != "/customer":
            continue
        if route.path in public_oauth_callbacks:
            continue
        names = _dependency_names(route)
        if not accepted.intersection(names):
            failures.append((sorted(route.methods), route.path, sorted(names)))
    assert failures == []


def test_customer_management_surfaces_require_manager_or_admin_role():
    protected_prefixes = (
        "/customer/agents",
        "/customer/business",
        "/customer/operations",
        "/customer/meta-whatsapp",
    )
    failures = []
    for route in _mounted_api_routes():
        if not route.path.startswith(protected_prefixes):
            continue
        names = _dependency_names(route)
        if not {"require_customer_manager", "require_customer_admin"}.intersection(names):
            failures.append((sorted(route.methods), route.path, sorted(names)))
    assert failures == []


def test_customer_inbox_is_operator_scoped_not_manager_scoped():
    failures = []
    for route in _mounted_api_routes():
        if not route.path.startswith("/customer/inbox"):
            continue
        names = _dependency_names(route)
        if "require_customer_operator" not in names:
            failures.append((sorted(route.methods), route.path, sorted(names)))
    assert failures == []


def test_usage_and_ai_agent_management_are_tenant_manager_scoped():
    failures = []
    for route in _mounted_api_routes():
        if not route.path.startswith(("/usage", "/ai-agents")):
            continue
        names = _dependency_names(route)
        if "require_customer_manager" not in names:
            failures.append((sorted(route.methods), route.path, sorted(names)))
    assert failures == []


def test_public_customer_oauth_callbacks_are_explicit_and_non_mutating():
    callbacks = {
        "/customer/meta/channels/instagram/oauth/callback",
    }
    mounted = {route.path: route for route in _mounted_api_routes()}
    for path in callbacks:
        route = mounted[path]
        assert route.methods == {"GET"}
        assert _dependency_names(route) == set()
