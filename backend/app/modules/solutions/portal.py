from __future__ import annotations


BUSINESS_CAPABILITY_MODULES = {
    "booking",
    "orders",
    "quotation",
    "lead_management",
    "customer_support",
}

CAPABILITY_PORTAL_ITEMS = (
    (
        "quotation",
        {
            "id": "requests-quotation",
            "label": "Quotation Requests",
            "loader": "business",
            "capability_module": "quotation",
            "group": "Customer Operations",
        },
    ),
    (
        "booking",
        {
            "id": "requests-booking",
            "label": "Bookings",
            "loader": "business",
            "capability_module": "booking",
            "group": "Customer Operations",
        },
    ),
    (
        "orders",
        {
            "id": "requests-orders",
            "label": "Orders & Requests",
            "loader": "business",
            "capability_module": "orders",
            "group": "Customer Operations",
        },
    ),
    (
        "lead_management",
        {
            "id": "requests-leads",
            "label": "Leads",
            "loader": "business",
            "capability_module": "lead_management",
            "group": "Customer Operations",
        },
    ),
    (
        "customer_support",
        {
            "id": "requests-support",
            "label": "Support Requests",
            "loader": "business",
            "capability_module": "customer_support",
            "include_handoffs": True,
            "group": "Customer Operations",
        },
    ),
)

SERVICE_PORTAL_REGISTRY = {
    "ai_agents": {
        "group": "AI Workforce",
        "items": [
            {
                "id": "agents",
                "label": "AI Employees",
                "loader": "agents",
                "group": "AI Workforce",
            },
            {
                "id": "chat",
                "label": "Test AI Employee",
                "loader": "chat",
                "group": "AI Workforce",
            },
            {
                "id": "usage",
                "label": "AI Usage",
                "loader": "usage",
                "group": "AI Workforce",
            },
            {
                "id": "conversations",
                "label": "Inbox",
                "loader": "conversations",
                "group": "Customer Operations",
            },
            {
                "id": "customers",
                "label": "Customers",
                "loader": "customers",
                "group": "Customer Operations",
            },
            {
                "id": "business-analytics",
                "label": "Business Analytics",
                "loader": "customer-analytics",
                "group": "Customer Operations",
            },
            {
                "id": "notifications",
                "label": "Notifications",
                "loader": "customer-notifications",
                "group": "Customer Operations",
            },
        ],
    },
    "automation": {
        "group": "Business Automation",
        "items": [
            {
                "id": "service-automation",
                "label": "Automation",
                "loader": "service",
                "service_code": "automation",
            }
        ],
    },
    "analytics": {
        "group": "Data & AI Analytics",
        "items": [
            {
                "id": "service-analytics",
                "label": "Analytics",
                "loader": "service",
                "service_code": "analytics",
            }
        ],
    },
    "integrations": {
        "group": "Connected Systems",
        "items": [
            {
                "id": "integrations",
                "label": "Connected Systems",
                "loader": "integrations",
                "service_code": "integrations",
            }
        ],
    },
}

SERVICE_ORDER = ("ai_agents", "automation", "analytics", "integrations")


def _item_with_group(item: dict, group: str, service_code: str | None = None) -> dict:
    value = {**item, "group": item.get("group") or group}
    if service_code and not value.get("service_code"):
        value["service_code"] = service_code
    return value


def build_customer_portal_navigation(
    active_service_codes: list[str] | set[str] | tuple[str, ...],
    enabled_modules: list[str] | set[str] | tuple[str, ...],
) -> list[dict]:
    active = {str(code).strip() for code in active_service_codes if str(code).strip()}
    modules = {str(name).strip() for name in enabled_modules if str(name).strip()}

    navigation = [
        {"id": "dashboard", "label": "Overview", "loader": "dashboard", "group": "Workspace"}
    ]

    if "ai_agents" in active:
        navigation.append(
            {
                "id": "business-profile",
                "label": "Business Profile",
                "loader": "business-profile",
                "group": "Company",
                "service_code": "ai_agents",
            }
        )

    ordered_services = [code for code in SERVICE_ORDER if code in active]
    ordered_services.extend(sorted(active.difference(ordered_services)))

    for service_code in ordered_services:
        definition = SERVICE_PORTAL_REGISTRY.get(service_code)
        if definition is None:
            navigation.append(
                {
                    "id": f"service-{service_code}",
                    "label": service_code.replace("_", " ").title(),
                    "loader": "service",
                    "service_code": service_code,
                    "group": service_code.replace("_", " ").title(),
                }
            )
            continue

        group = definition["group"]
        for item in definition["items"]:
            if service_code == "ai_agents" and item["id"] == "business-analytics":
                for capability, capability_item in CAPABILITY_PORTAL_ITEMS:
                    if capability in modules:
                        navigation.append(
                            _item_with_group(capability_item, group, "ai_agents")
                        )
            navigation.append(_item_with_group(item, group, service_code))

    navigation.append(
        {
            "id": "account",
            "label": "Account & Security",
            "loader": "account",
            "group": "Account",
        }
    )
    navigation.append(
        {"id": "billing", "label": "Billing", "loader": "billing", "group": "Account"}
    )
    return navigation
