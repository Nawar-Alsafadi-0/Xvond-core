from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from backend.app.modules.channels.catalog import (
    canonical_channel_type,
    list_customer_channel_capabilities,
)


COMPILER_VERSION = 5

GENERIC_PRIMITIVES = {
    "workflow_engine",
    "http_api",
    "webhook",
    "browser_web",
    "scheduler",
    "storage",
    "content_generation",
    "messaging",
    "human_approval",
}

# This catalog describes delivery defaults for common capabilities. It is never
# a whitelist of what a customer may request. Anything outside the catalog is
# composed by Xvond from generic primitives rather than marked unsupported.
REQUIREMENT_CATALOG: dict[str, dict[str, Any]] = {
    "human_handoff": {
        "kind": "tool",
        "status": "available",
        "delivery_mode": "native",
        "primitives": ["human_approval"],
    },
    "xvond_workspace": {
        "kind": "channel",
        "status": "available",
        "delivery_mode": "native",
        "primitives": ["messaging"],
    },
    "knowledge": {
        "kind": "knowledge",
        "status": "customer_input_required",
        "delivery_mode": "configure",
        "primitives": ["storage"],
    },
    "booking": {
        "kind": "module",
        "status": "xvond_build",
        "delivery_mode": "compose",
        "primitives": ["workflow_engine", "storage", "scheduler"],
    },
    "lead_management": {
        "kind": "module",
        "status": "xvond_build",
        "delivery_mode": "compose",
        "primitives": ["workflow_engine", "storage"],
    },
    "orders": {
        "kind": "module",
        "status": "xvond_build",
        "delivery_mode": "compose",
        "primitives": ["workflow_engine", "storage"],
    },
    "files": {
        "kind": "module",
        "status": "customer_input_required",
        "delivery_mode": "configure",
        "primitives": ["storage"],
    },
    "scheduling": {
        "kind": "automation",
        "status": "xvond_build",
        "delivery_mode": "compose",
        "primitives": ["scheduler", "workflow_engine"],
    },
    "web_research": {
        "kind": "tool",
        "status": "xvond_build",
        "delivery_mode": "compose",
        "primitives": ["browser_web", "workflow_engine"],
    },
    "email_read": {
        "kind": "integration",
        "status": "connection_required",
        "delivery_mode": "connect_and_compose",
        "primitives": ["workflow_engine", "messaging"],
    },
    "email_send": {
        "kind": "integration",
        "status": "connection_required",
        "delivery_mode": "connect_and_compose",
        "primitives": ["workflow_engine", "messaging"],
    },
    "instagram_read": {
        "kind": "integration",
        "status": "connection_required",
        "delivery_mode": "connect_and_compose",
        "primitives": ["workflow_engine", "messaging"],
    },
    "instagram_publish": {
        "kind": "integration",
        "status": "connection_required",
        "delivery_mode": "connect_and_compose",
        "primitives": ["workflow_engine", "content_generation", "messaging"],
    },
    "whatsapp": {
        "kind": "channel",
        "status": "connection_required",
        "delivery_mode": "connect",
        "primitives": ["messaging"],
    },
    "website": {
        "kind": "channel",
        "status": "connection_required",
        "delivery_mode": "connect",
        "primitives": ["messaging"],
    },
    "content_generation": {
        "kind": "tool",
        "status": "available",
        "delivery_mode": "native",
        "primitives": ["content_generation"],
    },
}

# Communication channels are product capabilities, not arbitrary compiler
# inventions. Keep their delivery truth in the channel registry and normalize
# them into the compiler requirement catalog here. Action integrations such as
# email_send and instagram_publish remain separate requirements above.
for _channel in list_customer_channel_capabilities():
    _key = str(_channel.get("type") or "").strip().lower()
    if not _key or _key == "xvond":
        continue
    REQUIREMENT_CATALOG.setdefault(
        _key,
        {
            "kind": "channel",
            "status": "connection_required",
            "delivery_mode": "connect",
            "primitives": ["messaging"],
        },
    )

_ALLOWED_KINDS = {
    "tool",
    "integration",
    "module",
    "channel",
    "automation",
    "knowledge",
    "permission",
    "custom",
}
_ALLOWED_PERMISSION_MODES = {"automatic", "ask_before", "never"}
_ALLOWED_EXECUTION_OPS = {"http_get_json", "extract", "compare", "notify"}
_ALLOWED_COMPARE_OPERATORS = {"lt", "lte", "gt", "gte", "eq", "neq"}


_SENSITIVE_RUNTIME_INPUT_KEYS = {
    "authorization",
    "password",
    "secret",
    "token",
    "api_key",
    "access_token",
    "refresh_token",
    "client_secret",
    "credential",
    "credentials",
}

def is_sensitive_requirement_key(value: Any) -> bool:
    """Return True when generic setup data must use a protected connection path."""

    key = normalize_requirement_key(value)
    if not key:
        return False
    if key in _SENSITIVE_RUNTIME_INPUT_KEYS:
        return True
    tokens = set(key.split("_"))
    if tokens.intersection({"password", "secret", "token", "credential", "credentials"}):
        return True
    return key.endswith("_api_key") or key.endswith("_access_key")


COMPILER_SYSTEM_PROMPT = """You are Xvond's AI Employee Compiler.
Your only task is to convert a customer's open-ended job brief into a structured employee specification and delivery plan.

The customer may be describing a company employee, personal assistant, monitoring agent, autonomous workflow worker, or a mixture. Never force the request into a predefined employee type.

Xvond follows a build-anything model for digital work: a requirement is not unsupported merely because there is no pre-existing named feature. Unknown digital capabilities should be composed from generic execution primitives such as workflow automation, APIs, web/browser work, scheduling, storage, messaging and approval gates. External services may still require the customer's account connection, credentials or permission.

Return STRICT JSON only. Do not use Markdown or code fences.
Use this shape:
{
  "role": "short human-readable role",
  "scope": "business|personal|hybrid",
  "summary": "one concise sentence describing the job",
  "intake": {
    "known": [
      {"key":"business_name","label":"Business name","value":"exact value already present in the Job Brief"}
    ],
    "missing": [
      {"key":"working_hours","label":"Working hours","purpose":"why this fact is genuinely required to configure the employee"}
    ]
  },
  "tasks": [
    {"name": "task name", "description": "what must happen", "trigger": "when it happens"}
  ],
  "requirements": [
    {
      "key": "stable_snake_case_key",
      "kind": "tool|integration|module|channel|automation|knowledge|permission|custom",
      "purpose": "why it is needed",
      "requires_connection": false,
      "fulfillment_mode": "xvond_internal|external_connection|auto",
      "customer_inputs": [],
      "primitives": ["workflow_engine"],
      "schedule": {"kind":"interval|daily|weekly","every_minutes":60,"hour":8,"minute":0,"weekdays":[0,1,2,3,4],"timezone":"Asia/Muscat","source_text":"exact cadence/time words copied from the customer Job Brief"},
      "runtime_inputs": {"url":"https://example.com/data","target_price":100},
      "execution_plan": [
        {"id":"fetch","op":"http_get_json","url_field":"url"},
        {"id":"value","op":"extract","source":"fetch","path":"price"},
        {"id":"condition","op":"compare","source":"value","operator":"lte","value_field":"target_price"},
        {"id":"notify","op":"notify","when":"condition","title":"Condition matched","message":"The monitored condition matched."}
      ]
    }
  ],
  "permissions": [
    {"action": "action description", "mode": "automatic|ask_before|never"}
  ],
  "setup_questions": ["only information, account access or credentials genuinely required from the customer"]
}

Rules:
- Preserve every meaningful part of the customer's job. Do not silently drop unusual requirements.
- The Job Brief may contain chronological OWNER REFINEMENT sections. Treat later explicit owner refinements as authoritative when they conflict with earlier wording, while preserving unrelated requirements.
- Act like a smart product builder, not a static form. Extract useful facts already present in the Job Brief into intake.known and ask only for genuinely required missing facts in intake.missing.
- Never put a field in intake.missing when the same fact is already present in the Job Brief. Examples include business name, brand name, working hours, services, prices, booking rules, target market, preferred tone, escalation contact or operating constraints.
- Do not ask for optional preferences that Xvond can safely default. Only request facts whose absence would make the requested employee materially incorrect, unable to perform the requested job, or unsafe to launch.
- Keep intake field keys stable snake_case identifiers. Labels should be short human-readable labels in the customer's language when practical.
- intake.known values must be copied from information explicitly present in the Job Brief. Never invent values. Do not place passwords, API keys, access tokens or other credentials in intake.known; credentials belong to protected connection flows.
- Never use a missing Xvond feature as a reason to reject the job. For a novel digital requirement, return it and give it useful generic primitives so Xvond can compose it.
- Text/content generation itself is a native employee capability and does not need a fake external action or execution_plan. When generated content must be published, sent, stored or otherwise acted on externally, represent that side effect as its own requirement (for example instagram_publish) and include content_generation in that side-effect requirement's primitives.
- For recurring pipelines such as "generate and publish every day", attach the schedule to the requirement that performs the real side effect and include every needed primitive there (for example content_generation + scheduler + messaging + workflow_engine). Do not emit a disconnected standalone scheduling requirement when it would separate one requested pipeline into pieces that cannot execute together.
- Set requires_connection=true only when the customer explicitly wants to use an existing external account/system, or the requested work inherently depends on one.
- For capabilities Xvond can provide internally (for example booking/reservations, simple lead capture, forms, lightweight records or schedules), prefer fulfillment_mode=xvond_internal when no external system is explicitly required. Do not force the customer to buy or connect a third-party system merely because one exists.
- For booking/reservations specifically: if the Job Brief names an existing booking/calendar/provider that must be used, set requires_connection=true and fulfillment_mode=external_connection. Otherwise use fulfillment_mode=xvond_internal and let Xvond provide the booking capability. For internal booking, require only missing operational facts needed to make real slots: working_days, opening_time, closing_time and slot_minutes. Put explicitly stated values in runtime_inputs and only absent values in customer_inputs.
- Use fulfillment_mode=auto only when the customer's wording genuinely requires a choice that cannot be safely defaulted; prefer a working Xvond-native default over asking unnecessary questions.
- Separate reading from acting where permissions differ, e.g. email_read and email_send.
- Customer-selected communication surfaces must be represented as kind=channel requirements using their channel key. Email as a conversation surface is key=email; reading/sending mailbox work remains email_read/email_send. Instagram DM as a conversation surface is key=instagram; publishing remains instagram_publish.
- Publishing, sending, purchasing, deleting, booking, changing external data, or other consequential external actions should normally use ask_before unless the customer's brief explicitly says to do them automatically.
- Monitoring and recurring work must include scheduling/workflow primitives.
- When the customer explicitly gives a recurring cadence or clock time, include a structured schedule on the requirement. Use kind=interval with every_minutes, kind=daily with hour/minute, or kind=weekly with weekdays (0=Monday..6=Sunday) plus hour/minute. Include timezone only when the customer explicitly gave one; otherwise Xvond will use the workspace timezone. Always include schedule.source_text copied verbatim from the Job Brief words that authorize that cadence/time.
- Do not invent a cadence, clock time, weekday, or timezone that the customer did not request.
- For recurring/background work the customer explicitly asked to happen automatically, use permission mode automatic for that exact capability/purpose. If automatic execution is not authorized, keep ask_before and Xvond will not schedule it.
- runtime_inputs may contain only simple scalar values explicitly present in the customer's Job Brief and required by execution_plan. Never invent runtime input values.
- If the job needs private data or an external account, include the relevant connection requirement and customer input.
- Do not claim a system or account is already connected.
- For xvond_build work, include execution_plan only when the job can be represented with the allowed runtime ops: http_get_json, extract, compare, notify.
- execution_plan is declarative data, never code. Do not emit Python, JavaScript, shell commands, SQL, arbitrary HTTP methods, headers, credentials or secrets.
- http_get_json reads an HTTPS JSON endpoint from a named customer/runtime detail field such as url.
- extract reads a dot-separated path from a previous step.
- compare evaluates a previous step against either a literal value or a named runtime detail field.
- notify creates an idempotent Xvond notification/report; it is not an external email/social/message send.
""".strip()


def build_compiler_user_message(*, job_brief: str, requested_channels: list[str] | tuple[str, ...]) -> str:
    channels = ", ".join(requested_channels) if requested_channels else "none selected yet"
    return (
        "CUSTOMER JOB BRIEF:\n"
        f"{job_brief.strip()}\n\n"
        "CUSTOMER-SELECTED CHANNELS:\n"
        f"{channels}\n\n"
        "Compile this exact request into the JSON employee specification and delivery plan."
    )


def _bounded_text(value: Any, *, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _bounded_string_list(values: Any, *, limit: int = 20, item_limit: int = 300) -> list[str]:
    result: list[str] = []
    for item in values or []:
        value = _bounded_text(item, limit=item_limit)
        if value and value not in result:
            result.append(value)
        if len(result) >= limit:
            break
    return result


def _extract_json(text: str) -> dict:
    raw = (text or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Employee compiler must return a JSON object")
    return value


def _normalized_primitives(values: Any, *, fallback: list[str]) -> list[str]:
    result = []
    for value in values or []:
        key = str(value or "").strip().lower().replace(" ", "_")
        if key in GENERIC_PRIMITIVES and key not in result:
            result.append(key)
    return result or list(fallback)


def _normalize_execution_plan(values: Any) -> list[dict]:
    """Normalize compiler-produced runtime steps into a small declarative DSL.

    This is intentionally not a general code-execution surface. Unknown ops,
    free-form code, headers, credentials and arbitrary HTTP methods are dropped.
    """
    result: list[dict] = []
    seen_ids: set[str] = set()
    for raw in values or []:
        if not isinstance(raw, dict):
            continue
        step_id = normalize_requirement_key(raw.get("id"))[:64]
        op = str(raw.get("op") or "").strip().lower()
        if not step_id or step_id in seen_ids or op not in _ALLOWED_EXECUTION_OPS:
            continue
        step: dict[str, Any] = {"id": step_id, "op": op}
        if op == "http_get_json":
            url_field = normalize_requirement_key(raw.get("url_field") or "url")[:80]
            if not url_field:
                continue
            step["url_field"] = url_field
        elif op == "extract":
            source = normalize_requirement_key(raw.get("source"))[:64]
            path = _bounded_text(raw.get("path"), limit=200)
            if not source or source not in seen_ids or not re.fullmatch(r"[A-Za-z0-9_\\-]+(?:\\.[A-Za-z0-9_\\-]+)*", path):
                continue
            step["source"] = source
            step["path"] = path
        elif op == "compare":
            source = normalize_requirement_key(raw.get("source"))[:64]
            operator = str(raw.get("operator") or "").strip().lower()
            value_field = normalize_requirement_key(raw.get("value_field"))[:80]
            literal = raw.get("value")
            if not source or source not in seen_ids or operator not in _ALLOWED_COMPARE_OPERATORS:
                continue
            step["source"] = source
            step["operator"] = operator
            if value_field:
                step["value_field"] = value_field
            elif isinstance(literal, (str, int, float, bool)) or literal is None:
                step["value"] = literal
            else:
                continue
        elif op == "notify":
            when = normalize_requirement_key(raw.get("when"))[:64]
            if when:
                if when not in seen_ids:
                    continue
                step["when"] = when
            step["title"] = _bounded_text(raw.get("title"), limit=200) or "Employee update"
            step["message"] = _bounded_text(raw.get("message"), limit=1000) or "The employee completed a monitored condition."
        seen_ids.add(step_id)
        result.append(step)
        if len(result) >= 20:
            break
    return result


def _normalize_schedule_spec(value: Any, *, job_brief: str) -> dict | None:
    if not isinstance(value, dict):
        return None
    kind = str(value.get("kind") or "").strip().lower()
    if kind not in {"interval", "daily", "weekly"}:
        return None

    source_text = _bounded_text(value.get("source_text"), limit=500)
    source = str(job_brief or "")
    if not source_text or source_text.casefold() not in source.casefold():
        return None

    if kind == "interval":
        try:
            every_minutes = int(value.get("every_minutes"))
        except (TypeError, ValueError):
            return None
        if every_minutes < 5 or every_minutes > 60 * 24 * 30:
            return None
        return {"kind": "interval", "every_minutes": every_minutes, "source_text": source_text}

    try:
        hour = int(value.get("hour"))
        minute = int(value.get("minute", 0))
    except (TypeError, ValueError):
        return None
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None

    result = {"kind": kind, "hour": hour, "minute": minute, "source_text": source_text}
    timezone = _bounded_text(value.get("timezone"), limit=100)
    if timezone:
        result["timezone"] = timezone

    if kind == "weekly":
        weekdays = []
        for raw in value.get("weekdays") or []:
            try:
                day = int(raw)
            except (TypeError, ValueError):
                continue
            if 0 <= day <= 6 and day not in weekdays:
                weekdays.append(day)
        if not weekdays:
            return None
        result["weekdays"] = sorted(weekdays)
    return result


def _grounded_runtime_inputs(value: Any, *, job_brief: str) -> dict:
    if not isinstance(value, dict):
        return {}
    source = str(job_brief or "").casefold()
    result: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = normalize_requirement_key(raw_key)[:80]
        if (
            not key
            or key in _SENSITIVE_RUNTIME_INPUT_KEYS
            or not isinstance(raw_value, (str, int, float, bool))
        ):
            continue
        rendered = str(raw_value).strip()
        if not rendered or rendered.casefold() not in source:
            continue
        result[key] = raw_value
        if len(result) >= 20:
            break
    return result


def _normalize_smart_intake(value: Any, *, job_brief: str) -> dict:
    """Normalize compiler intake into grounded known facts plus only missing fields."""

    if not isinstance(value, dict):
        return {"known": [], "missing": []}

    source = str(job_brief or "").casefold()
    known: list[dict] = []
    known_keys: set[str] = set()
    for item in value.get("known") or []:
        if not isinstance(item, dict):
            continue
        key = normalize_requirement_key(item.get("key"))
        if not key or key in known_keys or is_sensitive_requirement_key(key):
            continue
        raw_value = item.get("value")
        if not isinstance(raw_value, (str, int, float, bool)):
            continue
        rendered = str(raw_value).strip()
        if not rendered or rendered.casefold() not in source:
            continue
        known_keys.add(key)
        known.append(
            {
                "key": key,
                "label": _bounded_text(item.get("label"), limit=120)
                or key.replace("_", " "),
                "value": rendered,
            }
        )
        if len(known) >= 30:
            break

    missing: list[dict] = []
    missing_keys: set[str] = set()
    for item in value.get("missing") or []:
        if not isinstance(item, dict):
            continue
        key = normalize_requirement_key(item.get("key"))
        if (
            not key
            or key in known_keys
            or key in missing_keys
            or is_sensitive_requirement_key(key)
        ):
            continue
        missing_keys.add(key)
        missing.append(
            {
                "key": key,
                "label": _bounded_text(item.get("label"), limit=120)
                or key.replace("_", " "),
                "purpose": _bounded_text(item.get("purpose"), limit=500),
            }
        )
        if len(missing) >= 30:
            break

    return {"known": known, "missing": missing}


def normalize_requirement_key(value: Any) -> str:
    key = _bounded_text(value, limit=120).lower().replace(" ", "_")
    if key and not re.fullmatch(r"[a-z0-9][a-z0-9_\-]{0,127}", key):
        # Preserve novel non-English/punctuated keys through a stable wire-safe ID.
        return "capability_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
    return key


def normalize_compiled_spec(payload: dict, *, job_brief: str) -> dict:
    role = _bounded_text(payload.get("role"), limit=160) or "AI Employee"
    scope = str(payload.get("scope") or "hybrid").strip().lower()
    if scope not in {"business", "personal", "hybrid"}:
        scope = "hybrid"
    summary = _bounded_text(payload.get("summary"), limit=1000) or _bounded_text(job_brief, limit=1000)
    intake = _normalize_smart_intake(payload.get("intake"), job_brief=job_brief)
    intake_known = {
        str(item.get("key") or ""): item.get("value")
        for item in (intake.get("known") or [])
        if isinstance(item, dict) and str(item.get("key") or "")
    }
    intake_missing_keys = {
        str(item.get("key") or "")
        for item in (intake.get("missing") or [])
        if isinstance(item, dict) and str(item.get("key") or "")
    }

    tasks: list[dict] = []
    for item in payload.get("tasks") or []:
        if not isinstance(item, dict):
            continue
        name = _bounded_text(item.get("name"), limit=160)
        description = _bounded_text(item.get("description"), limit=1200)
        trigger = _bounded_text(item.get("trigger"), limit=500)
        if name or description:
            tasks.append({
                "name": name or "Task",
                "description": description or name,
                "trigger": trigger or "as requested",
            })
        if len(tasks) >= 30:
            break

    requirements: list[dict] = []
    seen_keys: set[str] = set()
    for item in payload.get("requirements") or []:
        if not isinstance(item, dict):
            continue
        key = normalize_requirement_key(item.get("key"))
        declared_kind = str(item.get("kind") or "custom").strip().lower()
        if declared_kind == "channel":
            key = canonical_channel_type(key)
            if key == "xvond":
                key = "xvond_workspace"
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        if declared_kind not in _ALLOWED_KINDS:
            declared_kind = "custom"
        catalog = REQUIREMENT_CATALOG.get(key)
        requires_connection = bool(item.get("requires_connection"))
        fulfillment_mode = str(item.get("fulfillment_mode") or "").strip().lower()
        if fulfillment_mode not in {"xvond_internal", "external_connection", "auto"}:
            fulfillment_mode = "external_connection" if requires_connection else "xvond_internal"
        if catalog:
            status = str(catalog["status"])
            delivery_mode = str(catalog["delivery_mode"])
            kind = str(catalog["kind"])
            primitives = _normalized_primitives(
                item.get("primitives"),
                fallback=list(catalog.get("primitives") or ["workflow_engine"]),
            )
        else:
            kind = declared_kind
            if requires_connection or declared_kind in {"integration", "channel"}:
                status = "connection_required"
                delivery_mode = "connect_and_compose"
            else:
                status = "xvond_build"
                delivery_mode = "compose"
            primitives = _normalized_primitives(
                item.get("primitives"),
                fallback=["workflow_engine"],
            )
        customer_inputs = _bounded_string_list(item.get("customer_inputs"))
        runtime_inputs = _grounded_runtime_inputs(
            item.get("runtime_inputs"),
            job_brief=job_brief,
        )

        # Internal booking gets a deterministic minimum operating contract. The
        # compiler may ground values from the brief; only missing values become
        # dynamic setup fields.
        if key == "booking" and not requires_connection:
            fulfillment_mode = "xvond_internal"
            for grounded_field in (
                "working_days",
                "opening_time",
                "closing_time",
                "slot_minutes",
                "capacity",
            ):
                if (
                    grounded_field not in runtime_inputs
                    and str(intake_known.get(grounded_field) or "").strip()
                ):
                    runtime_inputs[grounded_field] = intake_known[grounded_field]

            for booking_field in (
                "working_days",
                "opening_time",
                "closing_time",
                "slot_minutes",
            ):
                if booking_field not in runtime_inputs and booking_field not in customer_inputs:
                    customer_inputs.append(booking_field)

            if (
                {"booking_capacity", "capacity"} & intake_missing_keys
                and "capacity" not in runtime_inputs
                and "capacity" not in customer_inputs
            ):
                customer_inputs.append("capacity")

        after_input_status = None
        # Explicit customer prerequisites take precedence over build defaults.
        if requires_connection:
            status = "connection_required"
            delivery_mode = "connect_and_compose"
            fulfillment_mode = "external_connection"
        elif customer_inputs and status == "xvond_build":
            after_input_status = "xvond_build"
            status = "customer_input_required"
            delivery_mode = "configure"
        schedule = _normalize_schedule_spec(item.get("schedule"), job_brief=job_brief)
        if schedule:
            for primitive in ("scheduler", "workflow_engine"):
                if primitive not in primitives:
                    primitives.append(primitive)
        requirements.append({
            "key": key,
            "kind": kind,
            "purpose": _bounded_text(item.get("purpose"), limit=800),
            "status": status,
            "delivery_mode": delivery_mode,
            "primitives": primitives,
            "schedule": schedule,
            "runtime_inputs": runtime_inputs,
            "execution_plan": _normalize_execution_plan(item.get("execution_plan")),
            "customer_inputs": customer_inputs,
            "requires_connection": requires_connection,
            "fulfillment_mode": fulfillment_mode,
            "after_input_status": after_input_status,
            "known_to_xvond": bool(catalog),
        })
        if len(requirements) >= 50:
            break

    intake_missing = list(intake.get("missing") or [])
    internal_booking = next(
        (
            item for item in requirements
            if isinstance(item, dict)
            and item.get("key") == "booking"
            and item.get("fulfillment_mode") == "xvond_internal"
        ),
        None,
    )
    if internal_booking is not None:
        booking_owned_intake = {
            "working_hours",
            "working_days",
            "opening_time",
            "closing_time",
            "slot_minutes",
            "booking_capacity",
            "capacity",
        }
        intake_missing = [
            item
            for item in intake_missing
            if str(item.get("key") or "") not in booking_owned_intake
        ]

    if intake_missing and "employee_context" not in seen_keys:
        input_fields = [item["key"] for item in intake_missing]
        input_labels = {item["key"]: item["label"] for item in intake_missing}
        input_purposes = {
            item["key"]: item["purpose"]
            for item in intake_missing
            if item.get("purpose")
        }
        requirements.append(
            {
                "key": "employee_context",
                "kind": "knowledge",
                "purpose": "Complete only the missing facts required to configure this employee correctly.",
                "status": "customer_input_required",
                "delivery_mode": "configure",
                "primitives": ["storage"],
                "schedule": None,
                "runtime_inputs": {},
                "execution_plan": [],
                "customer_inputs": input_fields,
                "customer_input_labels": input_labels,
                "customer_input_purposes": input_purposes,
                "known_to_xvond": True,
            }
        )
        seen_keys.add("employee_context")

    permissions: list[dict] = []
    for item in payload.get("permissions") or []:
        if not isinstance(item, dict):
            continue
        action = _bounded_text(item.get("action"), limit=500)
        mode = str(item.get("mode") or "ask_before").strip().lower()
        if mode not in _ALLOWED_PERMISSION_MODES:
            mode = "ask_before"
        if action:
            permissions.append({"action": action, "mode": mode})
        if len(permissions) >= 50:
            break

    # setup_questions is presentation-only. Derive it from the normalized
    # smart intake so the UI never repeats a question for a fact already present
    # in the Job Brief. Connection requirements remain represented separately.
    setup_questions = []
    for item in intake_missing:
        label = str(item.get("label") or item.get("key") or "").strip()
        if label and label not in setup_questions:
            setup_questions.append(label)
        if len(setup_questions) >= 30:
            break

    return {
        "version": COMPILER_VERSION,
        "job_brief": job_brief.strip(),
        "role": role,
        "scope": scope,
        "summary": summary,
        "intake": intake,
        "tasks": tasks,
        "requirements": requirements,
        "permissions": permissions,
        "setup_questions": setup_questions,
        "ready_requirements": [x["key"] for x in requirements if x["status"] == "available"],
        "build_required": [x["key"] for x in requirements if x["status"] == "xvond_build"],
        "setup_required": [
            x["key"]
            for x in requirements
            if x["status"] in {"connection_required", "customer_input_required"}
        ],
        "unsupported_requirements": [],
    }


def parse_compiler_response(text: str, *, job_brief: str) -> dict:
    return normalize_compiled_spec(_extract_json(text), job_brief=job_brief)


def build_compiled_employee_system_prompt(*, owner_name: str, spec: dict) -> str:
    tasks = spec.get("tasks") or []
    task_lines = "\n".join(
        f"- {item.get('name', 'Task')}: {item.get('description', '')} (trigger: {item.get('trigger', 'as requested')})"
        for item in tasks
    ) or "- Follow the customer's job brief exactly."

    permissions = spec.get("permissions") or []
    permission_lines = "\n".join(
        f"- {item.get('action', 'action')}: {item.get('mode', 'ask_before')}"
        for item in permissions
    ) or "- Ask before consequential external actions unless the owner explicitly authorized automatic execution."

    requirements = spec.get("requirements") or []
    requirement_lines = "\n".join(
        f"- {item.get('key', 'requirement')}: {item.get('status', 'xvond_build')} — {item.get('purpose', '')}"
        for item in requirements
    ) or "- No additional requirements were identified."

    intake = spec.get("intake") if isinstance(spec.get("intake"), dict) else {}
    known_context_lines = "\n".join(
        f"- {item.get('label') or item.get('key')}: {item.get('value')}"
        for item in (intake.get("known") or [])
        if isinstance(item, dict) and str(item.get("value") or "").strip()
    ) or "- No additional facts were extracted from the Job Brief."

    customer_inputs = spec.get("customer_inputs") or {}
    rendered_customer_inputs: list[str] = []
    for key, value in customer_inputs.items():
        if isinstance(value, dict):
            fields = "; ".join(
                f"{str(field).replace('_', ' ')}={field_value}"
                for field, field_value in value.items()
                if str(field_value or "").strip()
            )
            if fields:
                rendered_customer_inputs.append(
                    f"- {str(key).replace('_', ' ')}: {fields}"
                )
        elif str(value or "").strip():
            rendered_customer_inputs.append(
                f"- {str(key).replace('_', ' ')}: {value}"
            )
    customer_input_lines = "\n".join(rendered_customer_inputs) or "- No additional owner-provided setup data."

    return f"""You are one persistent Xvond AI employee for {owner_name}.

ROLE:
{spec.get('role') or 'AI Employee'}

SCOPE:
{spec.get('scope') or 'hybrid'}

JOB BRIEF — SOURCE OF TRUTH:
{spec.get('job_brief') or ''}

JOB SUMMARY:
{spec.get('summary') or ''}

TASKS:
{task_lines}

PERMISSIONS:
{permission_lines}

REQUIREMENTS AND DELIVERY STATUS:
{requirement_lines}

KNOWN CONTEXT EXTRACTED FROM THE JOB BRIEF:
{known_context_lines}

OWNER-PROVIDED SETUP DATA:
{customer_input_lines}

OPERATING RULES:
- Treat the original job brief as authoritative. The structured specification helps you execute it; it does not narrow or replace it.
- Xvond may compose novel digital capabilities through the managed workflow execution plane. A capability does not need a pre-existing named product feature to belong in this employee.
- Use only tools, integrations, channels, automations and knowledge that are actually attached and available in the current runtime.
- connection_required means the owner must connect or authorize an external account. customer_input_required means the owner must provide required data or files.
- xvond_build means Xvond owns the build/provisioning work. Do not describe it as unsupported. Do not claim an external action succeeded until its runtime capability is actually provisioned and returns success.
- xvond_managed means an action contract is stored, not that its execution adapter is ready by itself. execution_status=ready means the runtime capability is executable. schedule_status=ready means Xvond has provisioned the recurring scheduler for that capability. Never infer a running schedule from a contract alone, and never claim external work happened without a successful runtime result.
- Follow the permission mode for each action. For ask_before actions, obtain approval before execution.
- Never invent emails, bookings, orders, prices, account data, analytics, files, external results or successful publishing.
- Preserve context across the employee's connected channels and avoid asking the owner to repeat known information.
- Match the user's language unless an explicit employee setting overrides it.
""".strip()
