INTEGRATION_CATALOG = {

    "email_smtp": {
        "name": "Email (SMTP)",
        "execution_adapter": "email_smtp",
        "requirement_keys": ["email_send"],
        "generic_requirements": False,
        "operation_endpoints": False,
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

    "email_imap": {
        "name": "Email Inbox (IMAP)",
        "execution_adapter": "email_imap",
        "requirement_keys": ["email_read"],
        "generic_requirements": False,
        "operation_endpoints": False,
        "description": "Read email securely from the customer's inbox without marking messages as read",
        "config_fields": [
            {
                "name": "imap_host",
                "label": "IMAP Host",
                "required": True,
                "secret": False,
            },
            {
                "name": "imap_port",
                "label": "IMAP Port",
                "required": True,
                "secret": False,
                "default": 993,
            },
            {
                "name": "username",
                "label": "IMAP Username",
                "required": True,
                "secret": False,
            },
            {
                "name": "password",
                "label": "IMAP Password / App Password",
                "required": True,
                "secret": True,
            },
            {
                "name": "mailbox",
                "label": "Mailbox",
                "required": False,
                "secret": False,
                "default": "INBOX",
            },
        ],
    },

    "instagram_publish": {
        "name": "Instagram Publishing",
        "execution_adapter": "instagram_publish",
        "requirement_keys": ["instagram_publish"],
        "generic_requirements": False,
        "operation_endpoints": False,
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
        "execution_adapter": "http_api",
        "requirement_keys": [],
        "generic_requirements": True,
        "operation_endpoints": True,
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
        "execution_adapter": "http_api",
        "requirement_keys": [],
        "generic_requirements": True,
        "operation_endpoints": True,
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
        "execution_adapter": "http_api",
        "requirement_keys": [],
        "generic_requirements": True,
        "operation_endpoints": True,
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
        "name": "Google Calendar",
        "execution_adapter": "google_calendar",
        "requirement_keys": ["booking"],
        "generic_requirements": False,
        "allow_generic_alternatives": True,
        "operation_endpoints": False,
        "packaged_operations": {
            "availability": {"adapter": "google_calendar"},
            "execute": {"adapter": "google_calendar"},
            "cancel": {"adapter": "google_calendar"},
        },
        "description": "Check availability and manage bookings in Google Calendar",
        "config_fields": [
            {
                "name": "provider",
                "label": "Provider",
                "required": False,
                "secret": False,
                "default": "google",
                "choices": ["google"],
            },
            {
                "name": "calendar_id",
                "label": "Calendar ID",
                "required": False,
                "secret": False,
                "default": "primary",
            },
            {
                "name": "timezone",
                "label": "Calendar Timezone",
                "required": True,
                "secret": False,
            },
            {
                "name": "slot_minutes",
                "label": "Booking Duration (minutes)",
                "required": False,
                "secret": False,
                "default": 30,
            },
            {
                "name": "access_token",
                "label": "Google OAuth Access Token",
                "required": False,
                "secret": True,
            },
            {
                "name": "refresh_token",
                "label": "Google OAuth Refresh Token",
                "required": False,
                "secret": True,
            },
            {
                "name": "client_id",
                "label": "Google OAuth Client ID",
                "required": False,
                "secret": False,
            },
            {
                "name": "client_secret",
                "label": "Google OAuth Client Secret",
                "required": False,
                "secret": True,
            },
        ],
    },

    "webhook": {
        "name": "Webhook",
        "execution_adapter": "webhook",
        "requirement_keys": [],
        "generic_requirements": True,
        "operation_endpoints": False,
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
        "execution_adapter": "http_api",
        "requirement_keys": [],
        "generic_requirements": True,
        "operation_endpoints": True,
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


def executable_integration_types() -> set[str]:
    return {
        key
        for key, definition in INTEGRATION_CATALOG.items()
        if str(definition.get("execution_adapter") or "").strip()
    }


def compatible_integration_types(requirement_key: str) -> set[str]:
    key = str(requirement_key or "").strip().lower()
    packaged = {
        integration_type
        for integration_type, definition in INTEGRATION_CATALOG.items()
        if key
        and key in {
            str(item or "").strip().lower()
            for item in (definition.get("requirement_keys") or [])
        }
        and str(definition.get("execution_adapter") or "").strip()
    }
    generic = {
        integration_type
        for integration_type, definition in INTEGRATION_CATALOG.items()
        if definition.get("generic_requirements") is True
        and str(definition.get("execution_adapter") or "").strip()
    }
    if packaged:
        allow_generic = any(
            definition.get("allow_generic_alternatives") is True
            for integration_type, definition in INTEGRATION_CATALOG.items()
            if integration_type in packaged
        )
        return packaged | generic if allow_generic else packaged
    return generic


def integration_packaged_operations(integration_type: str) -> dict:
    definition = get_integration_definition(str(integration_type or "").strip().lower())
    operations = definition.get("packaged_operations") if definition else None
    if not isinstance(operations, dict):
        return {}
    return {
        str(key): dict(value)
        for key, value in operations.items()
        if isinstance(value, dict)
    }


def integration_requires_operation_endpoints(integration_type: str) -> bool:
    definition = get_integration_definition(str(integration_type or "").strip().lower())
    return bool(definition and definition.get("operation_endpoints") is True)


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

        name = field["name"]

        value = config.get(
            name,
            field.get("default"),
        )

        if value is None or str(value).strip() == "":
            if field.get("required"):
                missing.append(name)
            continue

        choices = field.get("choices")
        if choices:
            normalized = str(value).strip().lower()
            allowed = {str(item).strip().lower() for item in choices}
            if normalized not in allowed:
                raise ValueError(
                    f"Invalid integration configuration for {name}: "
                    + ", ".join(sorted(allowed))
                )

    if missing:
        raise ValueError(
            "Missing integration configuration: "
            + ", ".join(missing)
        )

    if str(integration_type or "").strip().lower() == "calendar":
        access_token = str(config.get("access_token") or "").strip()
        refresh_ready = all(
            str(config.get(field) or "").strip()
            for field in ("refresh_token", "client_id", "client_secret")
        )
        if not access_token and not refresh_ready:
            raise ValueError(
                "Google Calendar requires an access token or OAuth refresh credentials"
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
