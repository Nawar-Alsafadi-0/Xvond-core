from __future__ import annotations

import json
from typing import Any


COMPILER_VERSION = 1

# This registry describes what Xvond can currently wire or prepare. It is not a
# list of jobs the customer is allowed to request. Unknown requirements remain
# in the specification as custom requirements instead of being discarded.
REQUIREMENT_CATALOG: dict[str, dict[str, str]] = {
    "human_handoff": {"kind": "tool", "status": "available"},
    "xvond_workspace": {"kind": "channel", "status": "available"},
    "knowledge": {"kind": "knowledge", "status": "setup_required"},
    "booking": {"kind": "module", "status": "setup_required"},
    "lead_management": {"kind": "module", "status": "setup_required"},
    "orders": {"kind": "module", "status": "setup_required"},
    "files": {"kind": "module", "status": "setup_required"},
    "scheduling": {"kind": "automation", "status": "setup_required"},
    "web_research": {"kind": "tool", "status": "setup_required"},
    "email_read": {"kind": "integration", "status": "connection_required"},
    "email_send": {"kind": "integration", "status": "connection_required"},
    "instagram_read": {"kind": "integration", "status": "connection_required"},
    "instagram_publish": {"kind": "integration", "status": "connection_required"},
    "whatsapp": {"kind": "channel", "status": "connection_required"},
    "website": {"kind": "channel", "status": "connection_required"},
    "content_generation": {"kind": "tool", "status": "setup_required"},
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
Your only task is to convert a customer's open-ended job brief into a structured employee specification.

The customer may be describing a company employee, personal assistant, monitoring agent, autonomous workflow worker, or a mixture. Never force the request into a predefined employee type.

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
    {"key": "stable_snake_case_key", "kind": "tool|integration|module|channel|automation|knowledge|permission|custom", "purpose": "why it is needed"}
  ],
  "permissions": [
    {"action": "action description", "mode": "automatic|ask_before|never"}
  ],
  "setup_questions": ["only information or credentials genuinely required before this job can work"]
}

Rules:
- Preserve every meaningful part of the customer's job. Do not silently drop unusual requirements.
- A requirement key may be something Xvond does not know yet; still return it with kind "custom" when needed.
- Separate reading from acting where permissions differ, e.g. email_read and email_send.
- Publishing, sending, purchasing, deleting, booking, changing external data, or other consequential external actions should normally use ask_before unless the customer's brief explicitly says to do them automatically.
- Monitoring and recurring work must include an automation requirement.
- If the job needs private data or an external account, include the relevant integration requirement.
- Do not claim a system or integration is already connected.
""".strip()


def build_compiler_user_message(*, job_brief: str, requested_channels: list[str] | tuple[str, ...]) -> str:
    channels = ", ".join(requested_channels) if requested_channels else "none selected yet"
    return (
        "CUSTOMER JOB BRIEF:\n"
        f"{job_brief.strip()}\n\n"
        "CUSTOMER-SELECTED CHANNELS:\n"
        f"{channels}\n\n"
        "Compile this exact request into the JSON employee specification."
    )


def _bounded_text(value: Any, *, limit: int) -> str:
    return str(value or "").strip()[:limit]


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
        requirements.append({
            "key": key,
            "kind": catalog["kind"] if catalog else declared_kind,
            "purpose": _bounded_text(item.get("purpose"), limit=800),
            "status": catalog["status"] if catalog else "custom_required",
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

    setup_questions: list[str] = []
    for item in payload.get("setup_questions") or []:
        value = _bounded_text(item, limit=500)
        if value and value not in setup_questions:
            setup_questions.append(value)
        if len(setup_questions) >= 30:
            break

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
        "setup_required": [x["key"] for x in requirements if x["status"] != "available"],
    }


def parse_compiler_response(text: str, *, job_brief: str) -> dict:
    return normalize_compiled_spec(_extract_json(text), job_brief=job_brief)
