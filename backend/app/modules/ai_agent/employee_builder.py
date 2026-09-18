from __future__ import annotations

from dataclasses import dataclass

from backend.app.modules.channels.catalog import (
    CHANNEL_RUNTIME_LIVE,
    CHANNEL_SETUP_INTERNAL,
    CHANNEL_SETUP_MANAGED,
    CHANNEL_SETUP_SELF_SERVICE,
    customer_channel_types,
    get_channel_capability,
)


SUPPORTED_CAPABILITIES = (
    "customer_support",
    "sales",
    "lead_capture",
    "booking",
    "orders",
    "web_research",
    "email",
    "files",
    "content",
    "scheduling",
    "custom_task",
)

SUPPORTED_CHANNELS = customer_channel_types()

ACTION_CAPABILITIES = {
    "sales",
    "lead_capture",
    "booking",
    "orders",
    "email",
    "scheduling",
}

# Employee Builder V1 must not attach the legacy booking/order/lead tools because
# Delivery Readiness intentionally blocks those old paths from going live. The
# only safe runtime tool assigned automatically is human handoff. Real business
# actions are configured later through Xvond's canonical Business Actions flow.
CAPABILITY_TOOLS = {
    "customer_support": ("human_handoff",),
    "sales": ("human_handoff",),
    "lead_capture": ("human_handoff",),
    "booking": ("human_handoff",),
    "orders": ("human_handoff",),
}

# Requested channels are persisted in AgentConfig first. A real AgentChannel row
# is created only when its required credentials/config are supplied, avoiding
# invalid placeholder rows that could interfere with later setup.
RUNTIME_CHANNELS: set[str] = set()

CAPABILITY_KEYWORDS = {
    "customer_support": (
        "support", "customer service", "customers", "customer", "reply",
        "respond", "questions", "عملاء", "العملاء", "زبائن", "الزبائن",
        "يرد", "رد", "خدمة العملاء", "اسئلة", "أسئلة",
    ),
    "sales": (
        "sales", "sell", "selling", "close", "leads", "lead", "follow up",
        "follow-up", "مبيعات", "بيع", "يبيع", "اقناع", "إقناع", "متابعة",
        "عملاء محتملين", "ليد",
    ),
    "lead_capture": (
        "lead", "leads", "prospect", "prospects", "collect customer",
        "capture", "عملاء محتملين", "بيانات العملاء", "جمع بيانات", "ليد",
    ),
    "booking": (
        "booking", "book", "appointment", "appointments", "reservation",
        "reserve", "حجز", "يحجز", "موعد", "مواعيد", "حجوزات",
    ),
    "orders": (
        "order", "orders", "purchase", "checkout", "buy", "طلبات", "طلب",
        "شراء", "يطلب", "اوردر", "أوردر",
    ),
    "web_research": (
        "research", "search the web", "search online", "find online", "web",
        "internet", "بحث", "يبحث", "الويب", "الانترنت", "الإنترنت", "يدور",
    ),
    "email": (
        "email", "emails", "mail", "inbox", "ايميل", "إيميل", "بريد",
        "البريد", "رسائل البريد",
    ),
    "files": (
        "file", "files", "pdf", "document", "documents", "spreadsheet",
        "excel", "ملف", "ملفات", "مستند", "مستندات", "بي دي اف", "اكسل",
    ),
    "content": (
        "content", "post", "posts", "caption", "social content", "video script",
        "محتوى", "بوست", "منشورات", "كابشن", "سكريبت", "صناعة المحتوى",
    ),
    "scheduling": (
        "every day", "daily", "weekly", "schedule", "scheduled", "recurring",
        "remind", "monitor", "كل يوم", "يوميا", "يوميًا", "اسبوعيا", "أسبوعيا",
        "مجدول", "جدولة", "ذكرني", "يراقب",
    ),
}

CHANNEL_KEYWORDS = {
    "xvond": (
        "xvond workspace", "inside xvond", "in xvond", "داخل xvond",
        "داخل اكسفوند", "داخل إكسفوند", "اكسفوند", "إكسفوند",
    ),
    "website": ("website", "web site", "site chat", "موقع", "الموقع", "ويب سايت"),
    "whatsapp": ("whatsapp", "واتساب", "واتس", "واتس اب", "واتساب بزنس"),
    "voice": (
        "voice", "phone call", "phone calls", "call customers", "telephone",
        "مكالمة", "مكالمات", "اتصال هاتفي", "يتصل", "هاتف", "صوتي",
    ),
    "telegram": ("telegram", "تيليغرام", "تلغرام", "تليجرام", "تيليجرام"),
    "instagram": (
        "instagram dm", "instagram dms", "instagram messages", "insta dm",
        "انستغرام", "انستا", "رسائل انستغرام", "رسائل انستا",
    ),
    "messenger": (
        "facebook messenger", "messenger", "facebook messages",
        "ماسنجر", "فيسبوك ماسنجر", "رسائل فيسبوك",
    ),
    "email": (
        "email channel", "email inbox", "reply by email", "emails", "mail",
        "ايميل", "إيميل", "بريد", "البريد",
    ),
    "sms": ("sms", "text message", "text messages", "رسائل نصية", "رسالة نصية"),
    "slack": ("slack", "سلاك"),
    "teams": ("microsoft teams", "ms teams", "teams chat", "تيمز", "مايكروسوفت تيمز"),
    "custom": (
        "custom channel", "custom api channel", "custom webhook channel",
        "قناة مخصصة", "قناة api", "ويب هوك مخصص",
    ),
}

BUSINESS_KEYWORDS = (
    "company", "business", "customers", "sales", "store", "shop", "agency",
    "شركة", "شركتنا", "عميل", "عملاء", "زبائن", "متجر", "محل", "وكالة",
    "مبيعات", "بزنس", "مشروعنا",
)

PERSONAL_KEYWORDS = (
    "for me", "my personal", "personal assistant", "my work", "my tasks",
    "إلي", "الي", "شخصي", "مساعد شخصي", "مهامي", "شغلي", "خاص فيني",
)


@dataclass(frozen=True)
class EmployeeBlueprint:
    name: str
    description: str
    audience: str
    capabilities: tuple[str, ...]
    channels: tuple[str, ...]
    permissions: dict[str, str]
    missing_information: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "audience": self.audience,
            "capabilities": list(self.capabilities),
            "channels": list(self.channels),
            "permissions": dict(self.permissions),
            "missing_information": list(self.missing_information),
        }


def _normalized(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


def _contains_any(text: str, values: tuple[str, ...]) -> bool:
    return any(value in text for value in values)


def _unique(items: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def _audience(text: str) -> str:
    business_hits = sum(1 for item in BUSINESS_KEYWORDS if item in text)
    personal_hits = sum(1 for item in PERSONAL_KEYWORDS if item in text)
    if personal_hits > business_hits:
        return "personal"
    if business_hits:
        return "business"
    return "general"


def missing_information_for(
    capabilities: tuple[str, ...],
    channels: tuple[str, ...],
) -> tuple[str, ...]:
    """Return setup requirements for the final user-selected employee blueprint."""
    missing: list[str] = []
    if any(item in capabilities for item in ("customer_support", "sales", "lead_capture", "booking", "orders", "files")):
        missing.append("knowledge")
    if any(item in capabilities for item in ("lead_capture", "booking", "orders")):
        missing.append("business_actions")
    if "web_research" in capabilities:
        missing.append("web_research_tool")
    if "email" in capabilities:
        missing.append("email_connection")
    if "scheduling" in capabilities:
        missing.append("automation")
    for channel in channels:
        if channel != "xvond":
            missing.append(f"connect_{channel}")
    return _unique(missing)


def build_employee_blueprint(description: str) -> EmployeeBlueprint:
    clean = " ".join((description or "").strip().split())
    if len(clean) < 8:
        raise ValueError("Describe what you want your employee to do in a little more detail")
    if len(clean) > 4000:
        raise ValueError("Employee description is too long")

    text = _normalized(clean)
    capabilities = _unique([
        capability
        for capability in SUPPORTED_CAPABILITIES
        if capability != "custom_task"
        and _contains_any(text, CAPABILITY_KEYWORDS.get(capability, ()))
    ])
    if not capabilities:
        capabilities = ("custom_task",)

    channels = _unique([
        channel
        for channel in SUPPORTED_CHANNELS
        if _contains_any(text, CHANNEL_KEYWORDS.get(channel, ()))
    ])

    audience = _audience(text)
    permissions = {
        capability: ("ask_before_action" if capability in ACTION_CAPABILITIES else "automatic")
        for capability in capabilities
    }

    return EmployeeBlueprint(
        name="My AI Employee",
        description=clean,
        audience=audience,
        capabilities=capabilities,
        channels=channels,
        permissions=permissions,
        missing_information=missing_information_for(capabilities, channels),
    )


def sanitize_capabilities(values: list[str] | None, fallback: tuple[str, ...]) -> tuple[str, ...]:
    if values is None:
        return fallback
    cleaned = _unique([
        str(item).strip().lower()
        for item in values
        if str(item).strip().lower() in SUPPORTED_CAPABILITIES
    ])
    return cleaned or fallback


def sanitize_channels(values: list[str] | None, fallback: tuple[str, ...]) -> tuple[str, ...]:
    if values is None:
        return fallback
    return _unique([
        str(item).strip().lower()
        for item in values
        if str(item).strip().lower() in SUPPORTED_CHANNELS
    ])


def runtime_tools_for(capabilities: tuple[str, ...]) -> tuple[str, ...]:
    tools: list[str] = []
    for capability in capabilities:
        tools.extend(CAPABILITY_TOOLS.get(capability, ()))
    return _unique(tools)


def runtime_channels_for(channels: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(channel for channel in channels if channel in RUNTIME_CHANNELS)


def build_employee_system_prompt(*, owner_name: str, blueprint: EmployeeBlueprint) -> str:
    capabilities = ", ".join(blueprint.capabilities)
    return f"""You are one persistent AI employee created in Xvond for {owner_name}.

YOUR JOB:
{blueprint.description}

YOUR CONFIGURED CAPABILITIES:
{capabilities}

OPERATING RULES:
- You are one employee with multiple capabilities, not separate role-specific agents.
- Work toward the owner's stated job and remain consistent across connected Xvond channels.
- Use only tools and actions that are actually available in the current runtime.
- Never claim an external action succeeded unless the connected tool confirms success.
- If an action needs approval, ask for approval before taking it.
- Use connected knowledge and files as authoritative sources for owner-specific facts.
- Do not invent prices, policies, availability, appointments, orders, contacts, or private facts.
- If necessary information or a connection is missing, explain the exact missing requirement briefly.
- Preserve context and avoid making the user repeat information already provided.
- Match the user's language unless a later employee setting explicitly overrides it.
""".strip()


def blueprint_readiness(blueprint: EmployeeBlueprint) -> dict:
    capability_status = {}
    for capability in blueprint.capabilities:
        if capability in {"customer_support", "sales", "content", "custom_task"}:
            capability_status[capability] = "conversational_ready"
        elif capability in {"lead_capture", "booking", "orders", "files"}:
            capability_status[capability] = "setup_required"
        else:
            capability_status[capability] = "planned"

    channel_status = {}
    for channel in blueprint.channels:
        capability = get_channel_capability(channel) or {}
        if capability.get("setup_mode") == CHANNEL_SETUP_INTERNAL:
            status = "ready"
        elif (
            capability.get("runtime_state") == CHANNEL_RUNTIME_LIVE
            and capability.get("setup_mode") == CHANNEL_SETUP_SELF_SERVICE
        ):
            status = "connect_required"
        elif (
            capability.get("runtime_state") == CHANNEL_RUNTIME_LIVE
            and capability.get("setup_mode") == CHANNEL_SETUP_MANAGED
        ):
            status = "xvond_managed_setup"
        else:
            status = "xvond_adapter_required"
        channel_status[channel] = status
    return {
        "capabilities": capability_status,
        "channels": channel_status,
    }
