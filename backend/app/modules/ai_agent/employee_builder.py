from __future__ import annotations

from dataclasses import dataclass
import re


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
)

SUPPORTED_CHANNELS = (
    "xvond",
    "website",
    "whatsapp",
    "instagram",
    "email",
)

# Capabilities that already map to real Xvond runtime tools. Builder V1 never
# creates fictional tool assignments for features that are not wired yet.
CAPABILITY_TOOLS = {
    "customer_support": ("human_handoff",),
    "sales": ("lead", "human_handoff"),
    "lead_capture": ("lead",),
    "booking": ("booking", "human_handoff"),
    "orders": ("order", "human_handoff"),
}

# These channel surfaces already exist in the core. Xvond portal chat is an
# internal surface and does not need an AgentChannel row.
RUNTIME_CHANNELS = {"website", "whatsapp"}

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
    "website": ("website", "web site", "site chat", "موقع", "الموقع", "ويب سايت"),
    "whatsapp": ("whatsapp", "واتساب", "واتس", "واتس اب", "واتساب بزنس"),
    "instagram": ("instagram", "insta", "dm", "dms", "انستغرام", "انستا", "إنستغرام"),
    "email": ("email", "emails", "mail", "ايميل", "إيميل", "بريد"),
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


def _name_for(capabilities: tuple[str, ...], audience: str) -> str:
    labels = {
        "customer_support": "Customer & Support Employee",
        "sales": "Sales Employee",
        "booking": "Booking Employee",
        "web_research": "Research Employee",
        "content": "Content Employee",
        "email": "Email Employee",
        "scheduling": "Operations Employee",
    }
    if audience == "personal" and not capabilities:
        return "My AI Employee"
    for capability in capabilities:
        if capability in labels:
            return labels[capability]
    return "AI Employee"


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
        if _contains_any(text, CAPABILITY_KEYWORDS.get(capability, ()))
    ])

    # A useful employee should always be able to converse, even when the user
    # describes a niche job that V1 does not classify yet.
    if not capabilities:
        capabilities = ("customer_support",)

    channels = _unique([
        channel
        for channel in SUPPORTED_CHANNELS
        if channel != "xvond" and _contains_any(text, CHANNEL_KEYWORDS.get(channel, ()))
    ])
    if not channels:
        channels = ("xvond",)

    audience = _audience(text)

    action_capabilities = {"sales", "lead_capture", "booking", "orders", "email", "scheduling"}
    permissions = {
        capability: ("ask_before_action" if capability in action_capabilities else "automatic")
        for capability in capabilities
    }

    missing: list[str] = []
    if any(item in capabilities for item in ("customer_support", "sales", "booking", "orders")):
        missing.append("knowledge")
    if "booking" in capabilities:
        missing.append("booking_rules")
    if "email" in capabilities:
        missing.append("email_connection")
    for channel in channels:
        if channel not in {"xvond", "website"}:
            missing.append(f"connect_{channel}")

    return EmployeeBlueprint(
        name=_name_for(capabilities, audience),
        description=clean,
        audience=audience,
        capabilities=capabilities,
        channels=channels,
        permissions=permissions,
        missing_information=_unique(missing),
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
    cleaned = _unique([
        str(item).strip().lower()
        for item in values
        if str(item).strip().lower() in SUPPORTED_CHANNELS
    ])
    return cleaned or fallback


def runtime_tools_for(capabilities: tuple[str, ...]) -> tuple[str, ...]:
    tools: list[str] = []
    for capability in capabilities:
        tools.extend(CAPABILITY_TOOLS.get(capability, ()))
    return _unique(tools)


def runtime_channels_for(channels: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(channel for channel in channels if channel in RUNTIME_CHANNELS)


def build_employee_system_prompt(
    *,
    owner_name: str,
    blueprint: EmployeeBlueprint,
) -> str:
    capabilities = ", ".join(blueprint.capabilities)
    return f"""You are a persistent AI employee created in Xvond for {owner_name}.

YOUR JOB:
{blueprint.description}

YOUR CONFIGURED CAPABILITIES:
{capabilities}

OPERATING RULES:
- Work toward the user's stated job and remain consistent across connected Xvond channels.
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
    capability_status = {
        capability: (
            "ready" if capability in CAPABILITY_TOOLS or capability == "files" else "planned"
        )
        for capability in blueprint.capabilities
    }
    channel_status = {
        channel: (
            "ready" if channel in {"xvond", "website"}
            else "connect_required" if channel == "whatsapp"
            else "planned"
        )
        for channel in blueprint.channels
    }
    return {
        "capabilities": capability_status,
        "channels": channel_status,
    }
