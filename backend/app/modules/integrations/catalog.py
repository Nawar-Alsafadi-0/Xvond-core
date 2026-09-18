INTEGRATION_CATALOG = {

    "pos": {
        "name": "POS",
        "customer_connectable": True,
        "execution_adapter": "http_api",
        "description": "Point of Sale system",
        "config_fields": [
            {
                "name": "base_url",
                "label": "API Base URL",
                "required": True,
                "secret": False,
            },
            {
                "name": "api_key",
                "label": "API Key",
                "required": False,
                "secret": True,
            },
        ],
    },

    "crm": {
        "name": "CRM",
        "customer_connectable": True,
        "execution_adapter": "http_api",
        "description": "Customer Relationship Management system",
        "config_fields": [
            {
                "name": "base_url",
                "label": "API Base URL",
                "required": True,
                "secret": False,
            },
            {
                "name": "api_key",
                "label": "API Key",
                "required": False,
                "secret": True,
            },
        ],
    },

    "erp": {
        "name": "ERP",
        "customer_connectable": True,
        "execution_adapter": "http_api",
        "description": "Enterprise Resource Planning system",
        "config_fields": [
            {
                "name": "base_url",
                "label": "API Base URL",
                "required": True,
                "secret": False,
            },
            {
                "name": "api_key",
                "label": "API Key",
                "required": False,
                "secret": True,
            },
        ],
    },

    "calendar": {
        "name": "Calendar",
        "customer_connectable": False,
        "execution_adapter": None,
        "description": "External booking/calendar system",
        "config_fields": [
            {
                "name": "provider",
                "label": "Provider",
                "required": True,
                "secret": False,
            },
            {
                "name": "calendar_id",
                "label": "Calendar ID",
                "required": False,
                "secret": False,
            },
            {
                "name": "access_token",
                "label": "Access Token",
                "required": False,
                "secret": True,
            },
        ],
    },

    "webhook": {
        "name": "Webhook",
        "customer_connectable": True,
        "execution_adapter": "webhook",
        "description": "Send business events to an external webhook",
        "config_fields": [
            {
                "name": "url",
                "label": "Webhook URL",
                "required": True,
                "secret": False,
            },
            {
                "name": "secret",
                "label": "Webhook Secret",
                "required": False,
                "secret": True,
            },
        ],
    },

    "custom_api": {
        "name": "Custom API",
        "customer_connectable": True,
        "execution_adapter": "http_api",
        "description": "Connect any external business API",
        "config_fields": [
            {
                "name": "base_url",
                "label": "Base URL",
                "required": True,
                "secret": False,
            },
            {
                "name": "api_key",
                "label": "API Key",
                "required": False,
                "secret": True,
            },
        ],
    },
}


def get_integration_definition(
    integration_type: str,
):
    return INTEGRATION_CATALOG.get(
        integration_type
    )


def list_integration_definitions():
    return [
        {
            "type": key,
            **value,
        }
        for key, value
        in INTEGRATION_CATALOG.items()
    ]


def validate_integration_config(
    integration_type: str,
    config: dict,
):
    definition = get_integration_definition(
        integration_type
    )

    if definition is None:
        raise ValueError(
            "Unsupported integration type: "
            + integration_type
        )

    missing = []

    for field in definition["config_fields"]:

        if not field.get("required"):
            continue

        name = field["name"]

        value = config.get(
            name,
            field.get("default"),
        )

        if value is None or str(value).strip() == "":
            missing.append(name)

    if missing:
        raise ValueError(
            "Missing integration configuration: "
            + ", ".join(missing)
        )

    return True


SELF_SERVICE_CONNECTABLE_TYPES = frozenset(
    key
    for key, value in INTEGRATION_CATALOG.items()
    if value.get("customer_connectable") is True
    and value.get("execution_adapter")
)

EXECUTABLE_HTTP_INTEGRATION_TYPES = frozenset(
    key
    for key, value in INTEGRATION_CATALOG.items()
    if value.get("execution_adapter") == "http_api"
)


def list_customer_connectable_definitions():
    return [
        {
            "type": key,
            "name": value["name"],
            "description": value["description"],
            "config_fields": [
                {
                    "name": field["name"],
                    "label": field["label"],
                    "required": bool(field.get("required")),
                    "secret": bool(field.get("secret")),
                    **(
                        {"default": field.get("default")}
                        if field.get("default") is not None
                        else {}
                    ),
                }
                for field in value.get("config_fields") or []
            ],
            "execution_adapter": value.get("execution_adapter"),
        }
        for key, value in INTEGRATION_CATALOG.items()
        if key in SELF_SERVICE_CONNECTABLE_TYPES
    ]
