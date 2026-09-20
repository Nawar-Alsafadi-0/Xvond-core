CHANNEL_RUNTIME_LIVE = "live"
CHANNEL_RUNTIME_ADAPTER_REQUIRED = "adapter_required"

CHANNEL_SETUP_SELF_SERVICE = "self_service"
CHANNEL_SETUP_MANAGED = "managed"
CHANNEL_SETUP_INTERNAL = "internal"

N8N_CHANNEL_ADAPTER = "n8n_channel_gateway"
N8N_MANAGED_CHANNEL_FIELDS = [
    {"name": "connection_key", "label": "Xvond Connection Key", "required": True, "secret": False},
    {"name": "provider_account_label", "label": "Connected Account", "required": False, "secret": False},
    {"name": "channel_instructions", "label": "Channel-only Instructions", "required": False, "secret": False},
]


MANAGED_PROVIDER_SETUP = {
    "telegram": {
        "provider_name": "Telegram Bot API",
        "account_label_placeholder": "e.g. Support Bot @company_support",
        "setup_note": "Create the bot in BotFather, then paste its token and a new webhook secret.",
        "callback_note": "Xvond registers and verifies the Telegram webhook during provisioning.",
        "provider_type": "telegram",
        "provider_path": "/webhook/xvond-telegram-provider",
        "inbound_path": "/webhook/xvond-telegram-inbound",
        "fields": [
            {"name": "bot_token", "label": "Telegram Bot Token", "required": True, "secret": True, "placeholder": "123456789:AA...", "help": "Token issued by BotFather."},
            {"name": "webhook_secret", "label": "Webhook Secret", "required": True, "secret": True, "placeholder": "Use a long random value", "help": "Used to authenticate Telegram webhook deliveries."},
        ],
    },
    "instagram": {
        "provider_name": "Meta / Instagram",
        "account_label_placeholder": "e.g. Brand Instagram @company",
        "setup_note": "Use a professional Instagram account linked to a Facebook Page and a Meta app with Instagram messaging permissions.",
        "callback_note": "After saving, add the displayed callback URL to the Meta app and subscribe the Instagram account to messages.",
        "provider_type": "meta",
        "provider_path": "/webhook/xvond-meta-messaging-provider",
        "inbound_path": "/webhook/xvond-meta-messaging",
        "fields": [
            {"name": "sender_id", "label": "Instagram Professional Account ID", "required": True, "secret": False, "placeholder": "Numeric Instagram account ID"},
            {"name": "access_token", "label": "Meta Access Token", "required": True, "secret": True, "placeholder": "Long-lived Page access token"},
            {"name": "app_secret", "label": "Meta App Secret", "required": True, "secret": True, "placeholder": "Meta app secret"},
            {"name": "graph_version", "label": "Graph API Version", "required": False, "secret": False, "default": "v26.0"},
        ],
    },
    "messenger": {
        "provider_name": "Meta / Messenger",
        "account_label_placeholder": "e.g. Company Facebook Page",
        "setup_note": "Use a Facebook Page connected to a Meta app with Messenger permissions.",
        "callback_note": "After saving, add the displayed callback URL to the Meta app and subscribe the Page to messages and messaging_postbacks.",
        "provider_type": "meta",
        "provider_path": "/webhook/xvond-meta-messaging-provider",
        "inbound_path": "/webhook/xvond-meta-messaging",
        "fields": [
            {"name": "sender_id", "label": "Facebook Page ID", "required": True, "secret": False},
            {"name": "access_token", "label": "Page Access Token", "required": True, "secret": True},
            {"name": "app_secret", "label": "Meta App Secret", "required": True, "secret": True},
            {"name": "graph_version", "label": "Graph API Version", "required": False, "secret": False, "default": "v26.0"},
        ],
    },
    "slack": {
        "provider_name": "Slack App",
        "account_label_placeholder": "e.g. Customer Support Workspace",
        "setup_note": "Install a Slack app with bot scopes in the target workspace, then provide its bot token and signing secret.",
        "callback_note": "After saving, use the displayed callback URL for Slack Events and Interactivity, then subscribe to the required message events.",
        "provider_type": "slack",
        "provider_path": "/webhook/xvond-slack-provider",
        "inbound_path": "/webhook/xvond-slack-inbound",
        "fields": [
            {"name": "bot_token", "label": "Slack Bot Token", "required": True, "secret": True},
            {"name": "signing_secret", "label": "Slack Signing Secret", "required": True, "secret": True},
            {"name": "team_id", "label": "Slack Team ID", "required": False, "secret": False},
        ],
    },
    "sms": {
        "provider_name": "Twilio Messaging",
        "account_label_placeholder": "e.g. Support SMS +1 555 0100",
        "setup_note": "Connect a Twilio phone number or Messaging Service. At least one outbound sender must be supplied.",
        "callback_note": "After saving, set the displayed callback URL as the incoming-message webhook in Twilio.",
        "provider_type": "sms",
        "provider_path": "/webhook/xvond-twilio-sms-provider",
        "inbound_path": "/webhook/xvond-twilio-sms-inbound",
        "fields": [
            {"name": "account_sid", "label": "Twilio Account SID", "required": True, "secret": False},
            {"name": "auth_token", "label": "Twilio Auth Token", "required": True, "secret": True},
            {"name": "from_number", "label": "Twilio From Number", "required": False, "secret": False},
            {"name": "messaging_service_sid", "label": "Messaging Service SID", "required": False, "secret": False},
        ],
    },
    "email": {
        "provider_name": "Mailgun Email",
        "account_label_placeholder": "e.g. support@mg.company.com",
        "setup_note": "Use a verified Mailgun sending domain and an inbound route for the recipient address.",
        "callback_note": "After saving, point the Mailgun inbound route and event webhook to the displayed callback URL.",
        "provider_type": "email",
        "provider_path": "/webhook/xvond-mailgun-email-provider",
        "inbound_path": "/webhook/xvond-mailgun-email-inbound",
        "fields": [
            {"name": "domain", "label": "Mailgun Domain", "required": True, "secret": False},
            {"name": "api_key", "label": "Mailgun API Key", "required": True, "secret": True},
            {"name": "webhook_signing_key", "label": "Mailgun Webhook Signing Key", "required": True, "secret": True},
            {"name": "from_address", "label": "From Address", "required": True, "secret": False},
            {"name": "recipient", "label": "Inbound Recipient", "required": False, "secret": False},
            {"name": "region", "label": "Mailgun Region", "required": False, "secret": False, "default": "us"},
        ],
    },
    "teams": {
        "provider_name": "Microsoft Bot Framework",
        "account_label_placeholder": "e.g. Company Support Teams Bot",
        "setup_note": "Use a Microsoft Bot registration installed in the target Teams tenant.",
        "callback_note": "After saving, configure the bot messaging endpoint with the displayed callback URL and install the Teams app package.",
        "provider_type": "teams",
        "provider_path": "/webhook/xvond-microsoft-teams-provider",
        "inbound_path": "/webhook/xvond-microsoft-teams-inbound",
        "fields": [
            {"name": "microsoft_app_id", "label": "Microsoft App ID", "required": True, "secret": False},
            {"name": "microsoft_app_password", "label": "Microsoft App Password", "required": True, "secret": True},
            {"name": "microsoft_tenant_id", "label": "Microsoft Tenant ID", "required": False, "secret": False, "default": "botframework.com"},
            {"name": "service_url", "label": "Bot Framework Service URL", "required": True, "secret": False},
        ],
    },
    "custom": {
        "provider_name": "Signed HTTPS Webhook",
        "account_label_placeholder": "e.g. Partner Support Channel",
        "setup_note": "Xvond sends signed outbound events to your HTTPS endpoint and verifies signed inbound events.",
        "callback_note": "After saving, send inbound events to the displayed callback URL using the configured signing secret.",
        "provider_type": "custom",
        "provider_path": "/webhook/xvond-custom-channel-provider",
        "inbound_path": "/webhook/xvond-custom-channel-inbound",
        "fields": [
            {"name": "outbound_url", "label": "Outbound HTTPS URL", "required": True, "secret": False},
            {"name": "inbound_secret", "label": "Inbound Signing Secret", "required": True, "secret": True},
            {"name": "outbound_secret", "label": "Outbound Shared Secret", "required": True, "secret": True},
        ],
    },
}


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
        "packaged_provider": True,
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
        "packaged_provider": True,
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
        "packaged_provider": True,
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
        "packaged_provider": True,
        "config_fields": [
            {"name": "provider", "label": "Voice Provider", "required": True, "secret": False},
            {"name": "phone_number", "label": "Phone Number", "required": True, "secret": False},
            {"name": "account_id", "label": "Account ID", "required": False, "secret": False},
            {"name": "auth_token", "label": "Auth Token", "required": False, "secret": True},
        ],
    },
    "telegram": {
        "name": "Telegram",
        "description": "Telegram bot messaging managed through Xvond",
        "setup_mode": CHANNEL_SETUP_MANAGED,
        "runtime_state": CHANNEL_RUNTIME_LIVE,
        "runtime_adapter": N8N_CHANNEL_ADAPTER,
        "customer_selectable": True,
        "channel_slot": True,
        "packaged_provider": True,
        "provider_setup": MANAGED_PROVIDER_SETUP["telegram"],
        "config_fields": list(N8N_MANAGED_CHANNEL_FIELDS),
    },
    "instagram": {
        "name": "Instagram DM",
        "description": "Instagram direct-message channel managed through Xvond",
        "setup_mode": CHANNEL_SETUP_MANAGED,
        "runtime_state": CHANNEL_RUNTIME_LIVE,
        "runtime_adapter": N8N_CHANNEL_ADAPTER,
        "customer_selectable": True,
        "channel_slot": True,
        "packaged_provider": True,
        "provider_setup": MANAGED_PROVIDER_SETUP["instagram"],
        "config_fields": list(N8N_MANAGED_CHANNEL_FIELDS),
    },
    "messenger": {
        "name": "Facebook Messenger",
        "description": "Facebook Page Messenger channel managed through Xvond",
        "setup_mode": CHANNEL_SETUP_MANAGED,
        "runtime_state": CHANNEL_RUNTIME_LIVE,
        "runtime_adapter": N8N_CHANNEL_ADAPTER,
        "customer_selectable": True,
        "channel_slot": True,
        "packaged_provider": True,
        "provider_setup": MANAGED_PROVIDER_SETUP["messenger"],
        "config_fields": list(N8N_MANAGED_CHANNEL_FIELDS),
    },
    "email": {
        "name": "Email",
        "description": "Inbound and outbound Mailgun email channel managed through Xvond",
        "setup_mode": CHANNEL_SETUP_MANAGED,
        "runtime_state": CHANNEL_RUNTIME_LIVE,
        "runtime_adapter": N8N_CHANNEL_ADAPTER,
        "customer_selectable": True,
        "channel_slot": True,
        "packaged_provider": True,
        "provider_setup": MANAGED_PROVIDER_SETUP["email"],
        "config_fields": list(N8N_MANAGED_CHANNEL_FIELDS),
    },
    "sms": {
        "name": "SMS",
        "description": "Twilio SMS messaging through the packaged Xvond managed-channel provider",
        "setup_mode": CHANNEL_SETUP_MANAGED,
        "runtime_state": CHANNEL_RUNTIME_LIVE,
        "runtime_adapter": N8N_CHANNEL_ADAPTER,
        "customer_selectable": True,
        "channel_slot": True,
        "packaged_provider": True,
        "provider_setup": MANAGED_PROVIDER_SETUP["sms"],
        "config_fields": list(N8N_MANAGED_CHANNEL_FIELDS),
    },
    "slack": {
        "name": "Slack",
        "description": "Slack workspace messaging channel managed through Xvond",
        "setup_mode": CHANNEL_SETUP_MANAGED,
        "runtime_state": CHANNEL_RUNTIME_LIVE,
        "runtime_adapter": N8N_CHANNEL_ADAPTER,
        "customer_selectable": True,
        "channel_slot": True,
        "packaged_provider": True,
        "provider_setup": MANAGED_PROVIDER_SETUP["slack"],
        "config_fields": list(N8N_MANAGED_CHANNEL_FIELDS),
    },
    "teams": {
        "name": "Microsoft Teams",
        "description": "Microsoft Teams Bot Framework messaging through the packaged Xvond managed-channel provider",
        "setup_mode": CHANNEL_SETUP_MANAGED,
        "runtime_state": CHANNEL_RUNTIME_LIVE,
        "runtime_adapter": N8N_CHANNEL_ADAPTER,
        "customer_selectable": True,
        "channel_slot": True,
        "packaged_provider": True,
        "provider_setup": MANAGED_PROVIDER_SETUP["teams"],
        "config_fields": list(N8N_MANAGED_CHANNEL_FIELDS),
    },
    "custom": {
        "name": "Custom / API Channel",
        "description": "Signed generic webhook/API channel using the packaged Xvond custom-channel protocol",
        "setup_mode": CHANNEL_SETUP_MANAGED,
        "runtime_state": CHANNEL_RUNTIME_LIVE,
        "runtime_adapter": N8N_CHANNEL_ADAPTER,
        "customer_selectable": True,
        "channel_slot": True,
        "packaged_provider": True,
        "provider_setup": MANAGED_PROVIDER_SETUP["custom"],
        "config_fields": list(N8N_MANAGED_CHANNEL_FIELDS),
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


def packaged_managed_channel_types() -> frozenset[str]:
    """Managed channels with a source-controlled Xvond provider binding."""

    return frozenset(
        item["type"]
        for item in list_customer_channel_capabilities()
        if item.get("runtime_state") == CHANNEL_RUNTIME_LIVE
        and item.get("setup_mode") == CHANNEL_SETUP_MANAGED
        and item.get("packaged_provider") is True
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

    if definition.get("runtime_adapter") == N8N_CHANNEL_ADAPTER:
        if str(config.get("provisioning_state") or "").strip().lower() != "connected":
            missing.append("provisioning_state")

    if missing:
        missing = list(dict.fromkeys(missing))
        raise ValueError("Missing channel configuration: " + ", ".join(missing))
    return True
