from __future__ import annotations

import json
from typing import Any


COMPILER_VERSION = 2

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
        "status": "xvond_build",
        "delivery_mode": "compose",
        "primitives": ["content_generation", "workflow_engine"],
    },
}

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
  "tasks": [
    {"name": "task name", "description": "what must happen", "trigger": "when it happens"}
  ],
  "requirements": [
    {
      "key": "stable_snake_case_key",
      "kind": "tool|integration|module|channel|automation|knowledge|permission|custom",
      "purpose": "why it is needed",
      "requires_connection": false,
      "customer_inputs": [],
      "primitives": ["workflow_engine"]
    }
  ],
  "permissions": [
    {"action": "action description", "mode": "automatic|ask_before|never"}
  ],
  "setup_questions": ["only information, account access or credentials genuinely required from the customer"]
}

Rules:
- Preserve every meaningful part of the customer's job. Do not silently drop unusual requirements.
- Never use a missing Xvond feature as a reason to reject the job. For a novel digital requirement, return it and give it useful generic primitives so Xvond can compose it.
- Set requires_connection=true only when the customer must connect an external account, grant access or provide credentials for the work to function.
- Separate reading from acting where permissions differ, e.g. email_read and email_send.
- Publishing, sending, purchasing, deleting, booking, changing external data, or other consequential external actions should normally use ask_before unless the customer's brief explicitly says to do them automatically.
- Monitoring and recurring work must include scheduling/workflow primitives.
- If the job needs private data or an external account, include the relevant connection requirement and customer input.
- Do not claim a system or account is already connected.
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


def normalize_compiled_spec(payload: dict, *, job_brief: str) -> dict:
    role = _bounded_text(payload.get("role"), limit=160) or "AI Employee"
    scope = str(payload.get("scope") or "hybrid").strip().lower()
    if scope not in {"business", "personal", "hybrid"}:
        scope = "hybrid"
    summary = _bounded_text(payload.get("summary"), limit=1000) or _bounded_text(job_brief, limit=1000)

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
        key = _bounded_text(item.get("key"), limit=120).lower().replace(" ", "_")
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        declared_kind = str(item.get("kind") or "custom").strip().lower()
        if declared_kind not in _ALLOWED_KINDS:
            declared_kind = "custom"
        catalog = REQUIREMENT_CATALOG.get(key)
        requires_connection = bool(item.get("requires_connection"))
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
        requirements.append({
            "key": key,
            "kind": kind,
            "purpose": _bounded_text(item.get("purpose"), limit=800),
            "status": status,
            "delivery_mode": delivery_mode,
            "primitives": primitives,
            "customer_inputs": _bounded_string_list(item.get("customer_inputs")),
            "known_to_xvond": bool(catalog),
        })
        if len(requirements) >= 50:
            break

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

    setup_questions = _bounded_string_list(payload.get("setup_questions"), limit=30, item_limit=500)

    return {
        "version": COMPILER_VERSION,
        "job_brief": job_brief.strip(),
        "role": role,
        "scope": scope,
        "summary": summary,
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

OPERATING RULES:
- Treat the original job brief as authoritative. The structured specification helps you execute it; it does not narrow or replace it.
- Xvond may compose novel digital capabilities through the managed workflow execution plane. A capability does not need a pre-existing named product feature to belong in this employee.
- Use only tools, integrations, channels, automations and knowledge that are actually attached and available in the current runtime.
- connection_required means the owner must connect or authorize an external account. customer_input_required means the owner must provide required data or files.
- xvond_build means Xvond owns the build/provisioning work. Do not describe it as unsupported. Do not claim an external action succeeded until its runtime capability is actually provisioned and returns success.
- Follow the permission mode for each action. For ask_before actions, obtain approval before execution.
- Never invent emails, bookings, orders, prices, account data, analytics, files, external results or successful publishing.
- Preserve context across the employee's connected channels and avoid asking the owner to repeat known information.
- Match the user's language unless an explicit employee setting overrides it.
""".strip()
