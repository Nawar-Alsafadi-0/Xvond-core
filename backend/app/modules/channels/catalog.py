CHANNEL_RUNTIME_LIVE = "live"
CHANNEL_RUNTIME_ADAPTER_REQUIRED = "adapter_required"

CHANNEL_SETUP_SELF_SERVICE = "self_service"
CHANNEL_SETUP_MANAGED = "managed"
CHANNEL_SETUP_INTERNAL = "internal"

N8N_CHANNEL_RUNTIME_ADAPTER = "n8n_channel_bridge"


CHANNEL_ALIASES = {
    "instagram_dm": "instagram",
    "instagram_dms": "instagram",
    "facebook_messenger": "messenger",
    "fb_messenger": "messenger",
    "microsoft_teams": "teams",
    "ms_teams": "teams",
    "phone": "voice",
    "web_chat": "website",
    "site_chat": "website",
}


INTERNAL_CHANNEL_CATALOG = {
    "xvond": {
        "name": "Xvond Workspace",
        "description": "Built-in Xvond employee workspace",
        "setup_mode": CHANNEL_SETUP_INTERNAL,
        "runtime_state": CHANNEL_RUNTIME_LIVE,
        "runtime_adapter": "xvond_workspace",
        "customer_selectable": True,
        "channel_slot": False,
    },
}


CHANNEL_CATALOG = {
    "whatsapp": {
        "name": "WhatsApp",
        "description": "WhatsApp Business Cloud API",
        "setup_mode": CHANNEL_SETUP_SELF_SERVICE,
        "runtime_state": CHANNEL_RUNTIME_LIVE,
        "runtime_adapter": "meta_whatsapp_cloud",
        "customer_selectable": True,
        "channel_slot": True,
        "config_fields": [
            {"name": "phone_number_id", "label": "Phone Number ID", "required": True, "secret": False},
            {"name": "access_token", "label": "Access Token", "required": True, "secret": True},
            {"name": "verify_token", "label": "Verify Token", "required": True, "secret": True},
            {"name": "app_secret", "label": "App Secret", "required": True, "secret": True},
            {"name": "graph_api_version", "label": "Graph API Version", "required": True, "secret": False, "default": "v26.0"},
            {"name": "tone", "label": "Tone Override", "required": False, "secret": False, "default": "professional_friendly"},
            {"name": "response_style", "label": "Response Style", "required": False, "secret": False, "default": "conversational"},
            {"name": "response_length", "label": "Response Length", "required": False, "secret": False, "default": "concise"},
            {"name": "emoji_style", "label": "Emoji Style", "required": False, "secret": False, "default": "minimal"},
            {"name": "channel_instructions", "label": "WhatsApp-only Instructions", "required": False, "secret": False},
        ],
    },
    "website": {
        "name": "Website Chat",
        "description": "AI chat widget for customer websites",
        "setup_mode": CHANNEL_SETUP_SELF_SERVICE,
        "runtime_state": CHANNEL_RUNTIME_LIVE,
        "runtime_adapter": "xvond_website_widget",
        "customer_selectable": True,
        "channel_slot": True,
        "config_fields": [
            {"name": "allowed_domain", "label": "Allowed Domain", "required": True, "secret": False},
            {"name": "widget_name", "label": "Widget Name", "required": False, "secret": False},
            {"name": "human_assistance_mode", "label": "Human Assistance", "required": False, "secret": False, "default": "direct_handoff"},
            {"name": "contact_phone", "label": "Contact Phone", "required": False, "secret": False},
            {"name": "contact_whatsapp", "label": "Contact WhatsApp", "required": False, "secret": False},
            {"name": "contact_email", "label": "Contact Email", "required": False, "secret": False},
            {"name": "contact_url", "label": "Contact / Booking URL", "required": False, "secret": False},
        ],
    },
    "voice": {
        "name": "Voice / Phone",
        "description": "AI voice calls managed through Xvond",
        "setup_mode": CHANNEL_SETUP_MANAGED,
        "runtime_state": CHANNEL_RUNTIME_LIVE,
        "runtime_adapter": "vapi",
        "customer_selectable": True,
        "channel_slot": True,
        "config_fields": [
            {"name": "provider", "label": "Voice Provider", "required": True, "secret": False},
            {"name": "phone_number", "label": "Phone Number", "required": True, "secret": False},
            {"name": "account_id", "label": "Account ID", "required": False, "secret": False},
            {"name": "auth_token", "label": "Auth Token", "required": False, "secret": True},
        ],
    },
    "telegram": {
        "name": "Telegram",
        "description": "Telegram bot messaging",
        "setup_mode": CHANNEL_SETUP_MANAGED,
        "runtime_state": CHANNEL_RUNTIME_LIVE,
        "runtime_adapter": N8N_CHANNEL_RUNTIME_ADAPTER,
        "customer_selectable": True,
        "channel_slot": True,
        # Provider credentials live in n8n, not Xvond Core.
        "config_fields": [],
    },
    "instagram": {
        "name": "Instagram DM",
        "description": "Instagram direct-message channel",
        "setup_mode": CHANNEL_SETUP_MANAGED,
        "runtime_state": CHANNEL_RUNTIME_LIVE,
        "runtime_adapter": N8N_CHANNEL_RUNTIME_ADAPTER,
        "customer_selectable": True,
        "channel_slot": True,
        "config_fields": [],
    },
    "messenger": {
        "name": "Facebook Messenger",
        "description": "Facebook Page Messenger channel",
        "setup_mode": CHANNEL_SETUP_MANAGED,
        "runtime_state": CHANNEL_RUNTIME_LIVE,
        "runtime_adapter": N8N_CHANNEL_RUNTIME_ADAPTER,
        "customer_selectable": True,
        "channel_slot": True,
        "config_fields": [],
    },
    "email": {
        "name": "Email",
        "description": "Inbound and outbound employee email channel",
        "setup_mode": CHANNEL_SETUP_MANAGED,
        "runtime_state": CHANNEL_RUNTIME_LIVE,
        "runtime_adapter": N8N_CHANNEL_RUNTIME_ADAPTER,
        "customer_selectable": True,
        "channel_slot": True,
        "config_fields": [],
    },
    "sms": {
        "name": "SMS",
        "description": "SMS messaging channel",
        "setup_mode": CHANNEL_SETUP_MANAGED,
        "runtime_state": CHANNEL_RUNTIME_LIVE,
        "runtime_adapter": N8N_CHANNEL_RUNTIME_ADAPTER,
        "customer_selectable": True,
        "channel_slot": True,
        "config_fields": [],
    },
    "slack": {
        "name": "Slack",
        "description": "Slack workspace messaging channel",
        "setup_mode": CHANNEL_SETUP_MANAGED,
        "runtime_state": CHANNEL_RUNTIME_LIVE,
        "runtime_adapter": N8N_CHANNEL_RUNTIME_ADAPTER,
        "customer_selectable": True,
        "channel_slot": True,
        "config_fields": [],
    },
    "teams": {
        "name": "Microsoft Teams",
        "description": "Microsoft Teams messaging channel",
        "setup_mode": CHANNEL_SETUP_MANAGED,
        "runtime_state": CHANNEL_RUNTIME_LIVE,
        "runtime_adapter": N8N_CHANNEL_RUNTIME_ADAPTER,
        "customer_selectable": True,
        "channel_slot": True,
        "config_fields": [],
    },
    "custom": {
        "name": "Custom / API Channel",
        "description": "Custom customer communication surface",
        "setup_mode": CHANNEL_SETUP_MANAGED,
        "runtime_state": CHANNEL_RUNTIME_LIVE,
        "runtime_adapter": N8N_CHANNEL_RUNTIME_ADAPTER,
        "customer_selectable": True,
        "channel_slot": True,
        # Provider endpoint/credentials live in the managed n8n route.
        "config_fields": [],
    },
}


def canonical_channel_type(channel_type: str | None) -> str:
    value = str(channel_type or "").strip().lower().replace("-", "_").replace(" ", "_")
    return CHANNEL_ALIASES.get(value, value)


def get_channel_definition(channel_type: str):
    return CHANNEL_CATALOG.get(canonical_channel_type(channel_type))


def get_channel_capability(channel_type: str):
    key = canonical_channel_type(channel_type)
    if key in INTERNAL_CHANNEL_CATALOG:
        return {"type": key, **INTERNAL_CHANNEL_CATALOG[key]}
    definition = CHANNEL_CATALOG.get(key)
    if definition is None:
        return None
    return {"type": key, **definition}


def list_channel_definitions():
    return [{"type": key, **value} for key, value in CHANNEL_CATALOG.items()]


def list_customer_channel_capabilities():
    items = [
        {"type": key, **value}
        for key, value in {**INTERNAL_CHANNEL_CATALOG, **CHANNEL_CATALOG}.items()
        if value.get("customer_selectable") is True
    ]
    return items


def customer_channel_types() -> tuple[str, ...]:
    return tuple(item["type"] for item in list_customer_channel_capabilities())


def live_self_service_channel_types() -> frozenset[str]:
    return frozenset(
        item["type"]
        for item in list_customer_channel_capabilities()
        if item.get("runtime_state") == CHANNEL_RUNTIME_LIVE
        and item.get("setup_mode") in {CHANNEL_SETUP_SELF_SERVICE, CHANNEL_SETUP_INTERNAL}
    )


def live_managed_channel_types() -> frozenset[str]:
    return frozenset(
        item["type"]
        for item in list_customer_channel_capabilities()
        if item.get("runtime_state") == CHANNEL_RUNTIME_LIVE
        and item.get("setup_mode") == CHANNEL_SETUP_MANAGED
    )


def validate_channel_config(channel_type: str, config: dict):
    key = canonical_channel_type(channel_type)
    definition = get_channel_definition(key)
    if definition is None:
        raise ValueError(f"Unsupported channel type: {channel_type}")

    missing = []
    for field in definition["config_fields"]:
        if not field.get("required"):
            continue
        name = field["name"]
        value = config.get(name, field.get("default"))
        if value is None or str(value).strip() == "":
            missing.append(name)

    # Vapi authenticates its dedicated OpenAI-compatible callback with the
    # per-channel llm_api_key. Other providers that use the generic voice-turn
    # endpoint must provide an explicit auth_token so that the public endpoint
    # can never become unauthenticated by configuration accident.
    if key == "voice":
        provider = str(config.get("provider") or "").strip().lower()
        if provider != "vapi" and not str(config.get("auth_token") or "").strip():
            missing.append("auth_token")

    if missing:
        missing = list(dict.fromkeys(missing))
        raise ValueError("Missing channel configuration: " + ", ".join(missing))
    return True
