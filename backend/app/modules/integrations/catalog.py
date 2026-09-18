INTEGRATION_CATALOG = {

    "email_smtp": {
        "name": "Email (SMTP)",
        "description": "Send email securely through the customer's SMTP provider",
        "config_fields": [
            {
                "name": "smtp_host",
                "label": "SMTP Host",
                "required": True,
                "secret": False,
            },
            {
                "name": "smtp_port",
                "label": "SMTP Port",
                "required": True,
                "secret": False,
                "default": 465,
            },
            {
                "name": "username",
                "label": "SMTP Username",
                "required": True,
                "secret": False,
            },
            {
                "name": "password",
                "label": "SMTP Password / App Password",
                "required": True,
                "secret": True,
            },
            {
                "name": "from_address",
                "label": "From Email",
                "required": True,
                "secret": False,
            },
        ],
    },

    "instagram_publish": {
        "name": "Instagram Publishing",
        "description": "Publish media and captions to an Instagram professional account",
        "config_fields": [
            {
                "name": "instagram_user_id",
                "label": "Instagram User ID",
                "required": True,
                "secret": False,
            },
            {
                "name": "access_token",
                "label": "Access Token",
                "required": True,
                "secret": True,
            },
        ],
    },

    "pos": {
        "name": "POS",
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
            {
                "name": "validation_endpoint",
                "label": "Validation Endpoint",
                "required": True,
                "secret": False,
            },
        ],
    },

    "crm": {
        "name": "CRM",
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
            {
                "name": "validation_endpoint",
                "label": "Validation Endpoint",
                "required": True,
                "secret": False,
            },
        ],
    },

    "erp": {
        "name": "ERP",
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
            {
                "name": "validation_endpoint",
                "label": "Validation Endpoint",
                "required": True,
                "secret": False,
            },
        ],
    },

    "calendar": {
        "name": "Calendar",
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
            {
                "name": "validation_endpoint",
                "label": "Validation Endpoint",
                "required": True,
                "secret": False,
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


def integration_validation_ready(config: dict | None) -> bool:
    """Return whether the current connection configuration was validated.

    Connection updates remove this private evidence. Runtime and launch checks
    can therefore fail closed without treating ordinary managed integrations as
    customer-validated connections.
    """

    if not isinstance(config, dict):
        return False
    evidence = config.get("_xvond_validation")
    return bool(
        isinstance(evidence, dict)
        and evidence.get("validated") is True
        and str(evidence.get("validated_at") or "").strip()
    )
