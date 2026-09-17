import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.app.core.config_secrets import reveal_config
from backend.app.core.database.connection import SessionLocal
from backend.app.core.dependencies import require_xvond_admin
from backend.app.models.company_module import CompanyModule
from backend.app.models.user import User
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.tools.business_models import ActionRequest
from backend.app.modules.tools.models import AgentToolAssignment

router = APIRouter(prefix="/admin/agent-actions", tags=["Xvond Admin - Agent Actions"])

VALID_REQUEST_STATUSES = {
    "awaiting_confirmation",
    "new",
    "pending_human",
    "in_progress",
    "confirmed",
    "processing",
    "completed",
    "cancelled",
}
VALID_DESTINATIONS = {"unconfigured", "xvond_internal", "human_handoff", "integration"}
VALID_AVAILABILITY = {"none", "xvond_schedule", "integration"}
BUSINESS_MODULES = {
    "booking": "Booking & Reservations",
    "orders": "Orders & Requests",
    "quotation": "Quotation",
    "lead_management": "Lead Management",
    "customer_support": "Customer Support",
}


def _field(key, label, field_type="text", role="", required=True):
    return {
        "key": key,
        "label": label,
        "type": field_type,
        "role": role,
        "required": required,
    }


def _action(
    key,
    label,
    module,
    description,
    fields,
    *,
    availability=None,
    destination=None,
):
    return {
        "key": key,
        "label": label,
        "module": module,
        "enabled": False,
        "description": description,
        "fields": fields,
        "confirmation_required": True,
        "availability": availability or {"mode": "none"},
        "destination": destination or {"type": "xvond_internal"},
    }


TEMPLATES = [
    {
        "id": "catering",
        "label": "Catering / Events",
        "business_types": ["catering / events", "catering", "events", "hospitality"],
        "actions": [
            _action(
                "catering_request",
                "Catering Request",
                "orders",
                "Collect and register a structured catering request for an event.",
                [
                    _field("customer_name", "Customer name"),
                    _field("phone", "Phone"),
                    _field("event_type", "Event type"),
                    _field("event_date", "Event date", "date", "date"),
                    _field("guest_count", "Guest count", "number"),
                    _field("location", "Location"),
                    _field("request", "Requested service / package"),
                    _field("notes", "Notes", required=False),
                ],
            ),
            _action(
                "catering_quote",
                "Catering Quote Request",
                "quotation",
                "Collect the information needed for the team or pricing engine to prepare a catering quotation.",
                [
                    _field("customer_name", "Customer name"),
                    _field("phone", "Phone"),
                    _field("event_type", "Event type"),
                    _field("event_date", "Event date", "date", "date"),
                    _field("guest_count", "Guest count", "number"),
                    _field("location", "Location"),
                    _field("requirements", "Requirements"),
                    _field("budget", "Budget", required=False),
                ],
            ),
            _action(
                "consultation_booking",
                "Consultation Booking",
                "booking",
                "Check real availability and book a consultation slot.",
                [
                    _field("customer_name", "Customer name"),
                    _field("phone", "Phone"),
                    _field("date", "Date", "date", "date"),
                    _field("time", "Time", "time", "time"),
                    _field("notes", "Notes", required=False),
                ],
                availability={
                    "mode": "xvond_schedule",
                    "date_field": "date",
                    "time_field": "time",
                    "schedule": {
                        "weekdays": [],
                        "start": "",
                        "end": "",
                        "slot_minutes": 30,
                        "capacity": 1,
                    },
                },
            ),
        ],
    },
    {
        "id": "salon",
        "label": "Salon / Beauty",
        "business_types": ["salon / beauty", "salon", "beauty"],
        "actions": [
            _action(
                "book_appointment",
                "Book Appointment",
                "booking",
                "Check real availability and create a salon appointment.",
                [
                    _field("customer_name", "Customer name"),
                    _field("phone", "Phone"),
                    _field("service", "Service"),
                    _field("date", "Date", "date", "date"),
                    _field("time", "Time", "time", "time"),
                    _field("staff", "Preferred specialist", required=False),
                ],
                availability={
                    "mode": "xvond_schedule",
                    "date_field": "date",
                    "time_field": "time",
                    "resource_field": "staff",
                    "schedule": {
                        "weekdays": [],
                        "start": "",
                        "end": "",
                        "slot_minutes": 30,
                        "capacity": 1,
                    },
                },
            ),
            _action(
                "callback_request",
                "Callback Request",
                "customer_support",
                "Register a request for the salon team to call the customer.",
                [
                    _field("customer_name", "Customer name"),
                    _field("phone", "Phone"),
                    _field("reason", "Reason"),
                ],
            ),
        ],
    },
    {
        "id": "restaurant",
        "label": "Restaurant / Cafe",
        "business_types": ["restaurant / cafe", "restaurant", "cafe"],
        "actions": [
            _action(
                "food_order",
                "Food Order",
                "orders",
                "Collect and register a customer food order.",
                [
                    _field("customer_name", "Customer name"),
                    _field("phone", "Phone"),
                    _field("items", "Items / quantities"),
                    _field("fulfillment", "Pickup or delivery"),
                    _field("address", "Delivery address", required=False),
                    _field("notes", "Notes", required=False),
                ],
            ),
            _action(
                "table_reservation",
                "Table Reservation",
                "booking",
                "Check configured table availability and create a reservation.",
                [
                    _field("customer_name", "Customer name"),
                    _field("phone", "Phone"),
                    _field("guest_count", "Guests", "number"),
                    _field("date", "Date", "date", "date"),
                    _field("time", "Time", "time", "time"),
                    _field("notes", "Notes", required=False),
                ],
                availability={
                    "mode": "xvond_schedule",
                    "date_field": "date",
                    "time_field": "time",
                    "schedule": {
                        "weekdays": [],
                        "start": "",
                        "end": "",
                        "slot_minutes": 30,
                        "capacity": 1,
                    },
                },
            ),
        ],
    },
    {
        "id": "hotel",
        "label": "Hotel / Hospitality",
        "business_types": ["hotel / hospitality", "hotel"],
        "actions": [
            _action(
                "reservation_request",
                "Reservation Request",
                "booking",
                "Collect and register a room reservation request. Use an external booking integration when live room availability is required.",
                [
                    _field("customer_name", "Guest name"),
                    _field("phone", "Phone"),
                    _field("check_in", "Check-in", "date"),
                    _field("check_out", "Check-out", "date"),
                    _field("guests", "Guests", "number"),
                    _field("room_type", "Room type", required=False),
                    _field("notes", "Notes", required=False),
                ],
            )
        ],
    },
    {
        "id": "clinic",
        "label": "Clinic / Medical Center",
        "business_types": ["clinic / medical center", "dental clinic", "clinic", "medical"],
        "actions": [
            _action(
                "book_appointment",
                "Book Appointment",
                "booking",
                "Check configured availability and create an appointment.",
                [
                    _field("customer_name", "Patient name"),
                    _field("phone", "Phone"),
                    _field("service", "Service / department"),
                    _field("date", "Date", "date", "date"),
                    _field("time", "Time", "time", "time"),
                    _field("doctor", "Preferred doctor", required=False),
                ],
                availability={
                    "mode": "xvond_schedule",
                    "date_field": "date",
                    "time_field": "time",
                    "resource_field": "doctor",
                    "schedule": {
                        "weekdays": [],
                        "start": "",
                        "end": "",
                        "slot_minutes": 30,
                        "capacity": 1,
                    },
                },
            ),
            _action(
                "callback_request",
                "Callback Request",
                "customer_support",
                "Register a non-clinical callback request for the team.",
                [
                    _field("customer_name", "Customer name"),
                    _field("phone", "Phone"),
                    _field("reason", "Reason"),
                ],
            ),
        ],
    },
    {
        "id": "real_estate",
        "label": "Real Estate",
        "business_types": ["real estate"],
        "actions": [
            _action(
                "property_lead",
                "Property Lead",
                "lead_management",
                "Capture and qualify a property buyer, seller or tenant lead.",
                [
                    _field("customer_name", "Customer name"),
                    _field("phone", "Phone"),
                    _field("interest", "Buy / rent / sell"),
                    _field("property_type", "Property type", required=False),
                    _field("area", "Preferred area", required=False),
                    _field("budget", "Budget", required=False),
                ],
            ),
            _action(
                "viewing_booking",
                "Property Viewing",
                "booking",
                "Check real availability and book a property viewing.",
                [
                    _field("customer_name", "Customer name"),
                    _field("phone", "Phone"),
                    _field("property", "Property"),
                    _field("date", "Date", "date", "date"),
                    _field("time", "Time", "time", "time"),
                ],
                availability={
                    "mode": "xvond_schedule",
                    "date_field": "date",
                    "time_field": "time",
                    "schedule": {
                        "weekdays": [],
                        "start": "",
                        "end": "",
                        "slot_minutes": 30,
                        "capacity": 1,
                    },
                },
            ),
        ],
    },
    {
        "id": "professional_services",
        "label": "Professional / Service Company",
        "business_types": [
            "professional services",
            "legal services",
            "financial / accounting services",
            "marketing / media",
            "technology / software",
            "home services",
            "maintenance / contracting",
            "automotive",
        ],
        "actions": [
            _action(
                "quote_request",
                "Request Quote",
                "quotation",
                "Collect the information required for a quotation.",
                [
                    _field("customer_name", "Customer name"),
                    _field("phone", "Phone"),
                    _field("email", "Email", "email", required=False),
                    _field("service", "Service needed"),
                    _field("requirements", "Requirements"),
                    _field("budget", "Budget", required=False),
                    _field("timeline", "Timeline", required=False),
                ],
            ),
            _action(
                "consultation_booking",
                "Consultation Booking",
                "booking",
                "Check real availability and book a consultation.",
                [
                    _field("customer_name", "Customer name"),
                    _field("phone", "Phone"),
                    _field("date", "Date", "date", "date"),
                    _field("time", "Time", "time", "time"),
                    _field("topic", "Topic"),
                ],
                availability={
                    "mode": "xvond_schedule",
                    "date_field": "date",
                    "time_field": "time",
                    "schedule": {
                        "weekdays": [],
                        "start": "",
                        "end": "",
                        "slot_minutes": 30,
                        "capacity": 1,
                    },
                },
            ),
            _action(
                "service_lead",
                "Service Lead",
                "lead_management",
                "Capture a qualified service lead for follow-up.",
                [
                    _field("customer_name", "Customer name"),
                    _field("phone", "Phone"),
                    _field("service", "Service"),
                    _field("requirements", "Requirements", required=False),
                ],
            ),
        ],
    },
    {
        "id": "ecommerce",
        "label": "Retail / E-commerce",
        "business_types": ["retail store", "e-commerce", "pharmacy"],
        "actions": [
            _action(
                "customer_order",
                "Customer Order",
                "orders",
                "Collect and register a customer order or execute it through a connected commerce/POS API.",
                [
                    _field("customer_name", "Customer name"),
                    _field("phone", "Phone"),
                    _field("items", "Items / quantities"),
                    _field("fulfillment", "Pickup or delivery", required=False),
                    _field("address", "Delivery address", required=False),
                    _field("notes", "Notes", required=False),
                ],
            ),
            _action(
                "sales_lead",
                "Sales Lead",
                "lead_management",
                "Capture an interested customer for follow-up.",
                [
                    _field("customer_name", "Customer name"),
                    _field("phone", "Phone"),
                    _field("interest", "Interest"),
                ],
            ),
        ],
    },
    {
        "id": "general",
        "label": "General Business",
        "business_types": ["other"],
        "actions": [
            _action(
                "customer_request",
                "Customer Request",
                "customer_support",
                "Collect and register a structured customer request.",
                [
                    _field("customer_name", "Customer name"),
                    _field("phone", "Phone"),
                    _field("request", "Request"),
                    _field("notes", "Notes", required=False),
                ],
            )
        ],
    },
]


class AgentActionsPayload(BaseModel):
    template_id: str | None = None
    actions: list[dict] | None = None
    booking_mode: str | None = None
    booking_fields: list[str] = Field(default_factory=list)
    order_mode: str | None = None
    order_fields: list[str] = Field(default_factory=list)
    lead_enabled: bool | None = None
    lead_fields: list[str] = Field(default_factory=list)


class RequestStatusUpdate(BaseModel):
    status: str


def _assignment(db, agent_id, name):
    return (
        db.query(AgentToolAssignment)
        .filter(
            AgentToolAssignment.agent_id == agent_id,
            AgentToolAssignment.tool_name == name,
        )
        .first()
    )


def _set(db, agent_id, name, enabled, config):
    row = _assignment(db, agent_id, name)
    if row:
        row.enabled = enabled
        row.config = config
    else:
        db.add(
            AgentToolAssignment(
                agent_id=agent_id,
                tool_name=name,
                enabled=enabled,
                config=config,
            )
        )


def _slug(value: str) -> str:
    value = re.sub(
        r"[^a-zA-Z0-9_]+",
        "_",
        str(value or "").strip().lower(),
    ).strip("_")
    return value[:80]


def _normalize_fields(raw) -> list[dict]:
    result = []
    seen = set()
    for item in raw or []:
        if isinstance(item, str):
            key = _slug(item)
            data = {
                "key": key,
                "label": item.strip().replace("_", " "),
                "type": "text",
                "role": "",
                "required": True,
            }
        elif isinstance(item, dict):
            key = _slug(item.get("key") or item.get("name"))
            data = {
                "key": key,
                "label": str(item.get("label") or key.replace("_", " ")).strip()[:120],
                "type": str(item.get("type") or "text").strip()[:30],
                "role": str(item.get("role") or "").strip()[:30],
                "required": bool(item.get("required", True)),
            }
        else:
            continue
        if key and key not in seen:
            seen.add(key)
            result.append(data)
    return result[:50]


def _normalize_action(item: dict) -> dict:
    key = _slug(item.get("key") or item.get("name") or item.get("label"))
    if not key:
        raise HTTPException(400, "Each operation needs a key or name")

    module = _slug(item.get("module"))
    if module and module not in BUSINESS_MODULES:
        raise HTTPException(400, f"Unsupported business module for {key}")

    destination = dict(item.get("destination") or {})
    destination_type = str(destination.get("type") or "unconfigured").strip()
    if destination_type not in VALID_DESTINATIONS:
        raise HTTPException(400, f"Invalid destination for {key}")
    destination["type"] = destination_type

    availability = dict(item.get("availability") or {})
    availability_mode = str(availability.get("mode") or "none").strip()
    if availability_mode not in VALID_AVAILABILITY:
        raise HTTPException(400, f"Invalid availability mode for {key}")
    availability["mode"] = availability_mode

    fields = _normalize_fields(item.get("fields") or item.get("required_fields") or [])
    return {
        "key": key,
        "label": str(item.get("label") or key.replace("_", " ")).strip()[:150],
        "module": module,
        "description": str(item.get("description") or "").strip()[:1500],
        "enabled": bool(item.get("enabled", False)),
        "fields": fields,
        "confirmation_required": bool(item.get("confirmation_required", True)),
        "availability": availability,
        "destination": destination,
    }


def _enabled_business_modules(db, company_id: int) -> set[str]:
    rows = (
        db.query(CompanyModule)
        .filter(
            CompanyModule.company_id == company_id,
            CompanyModule.enabled.is_(True),
            CompanyModule.module_name.in_(list(BUSINESS_MODULES)),
        )
        .all()
    )
    return {row.module_name for row in rows}


def _readiness(action: dict, enabled_modules: set[str] | None = None) -> list[str]:
    issues = []
    if not action.get("enabled", False):
        return issues

    module = str(action.get("module") or "").strip()
    if not module:
        issues.append("Choose the capability module that owns this operation")
    elif enabled_modules is not None and module not in enabled_modules:
        issues.append(f"Enable {BUSINESS_MODULES.get(module, module)} for this company")

    destination = action.get("destination") or {}
    if destination.get("type") == "workflow_engine":
        issues.append("Xvond must configure an execution adapter for this generated action contract")
    if destination.get("type") == "unconfigured":
        issues.append("Choose a real destination")
    if destination.get("type") == "integration" and not destination.get("integration_id"):
        issues.append("Choose a connected integration")

    availability = action.get("availability") or {}
    if availability.get("mode") == "xvond_schedule":
        schedule = availability.get("schedule") or {}
        if not availability.get("date_field") or not availability.get("time_field"):
            issues.append("Choose date/time fields for availability")
        if not schedule.get("weekdays") or not schedule.get("start") or not schedule.get("end"):
            issues.append("Configure working days and hours")
    if availability.get("mode") == "integration" and destination.get("type") != "integration":
        issues.append("Integration availability requires an integration destination")
    return issues


def _legacy_actions(data: AgentActionsPayload) -> list[dict]:
    actions = []
    if data.booking_mode and data.booking_mode != "disabled":
        actions.append(
            {
                "key": "booking",
                "label": "Booking",
                "module": "booking",
                "enabled": True,
                "fields": data.booking_fields,
                "confirmation_required": True,
                "availability": {"mode": "none"},
                "destination": {
                    "type": "human_handoff"
                    if data.booking_mode == "human_handoff"
                    else "xvond_internal"
                },
            }
        )
    if data.order_mode and data.order_mode != "disabled":
        actions.append(
            {
                "key": "order",
                "label": "Order",
                "module": "orders",
                "enabled": True,
                "fields": data.order_fields,
                "confirmation_required": True,
                "availability": {"mode": "none"},
                "destination": {
                    "type": "human_handoff"
                    if data.order_mode == "human_handoff"
                    else "xvond_internal"
                },
            }
        )
    if data.lead_enabled:
        actions.append(
            {
                "key": "lead",
                "label": "Lead",
                "module": "lead_management",
                "enabled": True,
                "fields": data.lead_fields or ["name", "phone"],
                "confirmation_required": False,
                "availability": {"mode": "none"},
                "destination": {"type": "xvond_internal"},
            }
        )
    return actions


@router.get("/templates/catalog")
def templates_catalog(current_admin: User = Depends(require_xvond_admin)):
    return {
        "templates": TEMPLATES,
        "business_modules": [
            {"name": name, "label": label}
            for name, label in BUSINESS_MODULES.items()
        ],
    }


@router.get("/{agent_id}")
def get_actions(agent_id: int, current_admin: User = Depends(require_xvond_admin)):
    db = SessionLocal()
    try:
        agent = db.query(AIAgent).filter(AIAgent.id == agent_id).first()
        if not agent:
            raise HTTPException(404, "AI employee not found")

        enabled_modules = _enabled_business_modules(db, agent.company_id)
        assignment = _assignment(db, agent_id, "action_request")
        config = reveal_config(assignment.config) if assignment else {}
        stored = (config or {}).get("actions") or {}
        actions = []
        for key, value in stored.items():
            if isinstance(value, dict):
                item = {"key": key, **value}
                item["readiness_issues"] = _readiness(item, enabled_modules)
                actions.append(item)

        enabled_actions = [x for x in actions if x.get("enabled", False)]
        return {
            "agent_id": agent_id,
            "template_id": (config or {}).get("template_id"),
            "actions": actions,
            "enabled_modules": sorted(enabled_modules),
            "ready": bool(enabled_actions)
            and all(not x.get("readiness_issues") for x in enabled_actions),
        }
    finally:
        db.close()


@router.put("/{agent_id}")
def update_actions(
    agent_id: int,
    data: AgentActionsPayload,
    current_admin: User = Depends(require_xvond_admin),
):
    db = SessionLocal()
    try:
        agent = db.query(AIAgent).filter(AIAgent.id == agent_id).first()
        if not agent:
            raise HTTPException(404, "AI employee not found")

        raw_actions = data.actions if data.actions is not None else _legacy_actions(data)
        normalized = [_normalize_action(x) for x in raw_actions]
        enabled_modules = _enabled_business_modules(db, agent.company_id)

        for operation in normalized:
            if not operation.get("enabled", False):
                continue
            issues = _readiness(operation, enabled_modules)
            module_issues = [
                x
                for x in issues
                if x.startswith("Choose the capability module") or x.startswith("Enable ")
            ]
            if module_issues:
                raise HTTPException(409, f"{operation['label']}: {'; '.join(module_issues)}")

        by_key = {
            x["key"]: {k: v for k, v in x.items() if k != "key"}
            for x in normalized
        }
        _set(
            db,
            agent_id,
            "action_request",
            bool(any(x.get("enabled", False) for x in normalized)),
            {
                "approval_required": False,
                "template_id": data.template_id,
                "actions": by_key,
            },
        )

        # Legacy specialized tools must never compete with the generic operation engine.
        for legacy in ("booking", "order", "lead"):
            row = _assignment(db, agent_id, legacy)
            if row:
                row.enabled = False

        _set(db, agent_id, "human_handoff", True, {"approval_required": False})
        db.commit()
        return {
            "status": "updated",
            "enabled_modules": sorted(enabled_modules),
            "actions": [
                {**x, "readiness_issues": _readiness(x, enabled_modules)}
                for x in normalized
            ],
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.get("/companies/{company_id}/requests")
def list_requests(
    company_id: int,
    agent_id: int | None = None,
    current_admin: User = Depends(require_xvond_admin),
):
    db = SessionLocal()
    try:
        query = db.query(ActionRequest).filter(ActionRequest.company_id == company_id)
        if agent_id is not None:
            query = query.filter(ActionRequest.agent_id == agent_id)
        items = query.order_by(ActionRequest.id.desc()).limit(500).all()
        return {
            "requests": [
                {
                    "id": x.id,
                    "agent_id": x.agent_id,
                    "conversation_id": x.conversation_id,
                    "action_type": x.action_type,
                    "details": x.details,
                    "summary": x.summary,
                    "status": x.status,
                    "created_at": x.created_at,
                }
                for x in items
            ]
        }
    finally:
        db.close()


@router.patch("/requests/{request_id}")
def update_request_status(
    request_id: int,
    data: RequestStatusUpdate,
    current_admin: User = Depends(require_xvond_admin),
):
    if data.status not in VALID_REQUEST_STATUSES:
        raise HTTPException(400, "Invalid request status")
    db = SessionLocal()
    try:
        item = db.query(ActionRequest).filter(ActionRequest.id == request_id).first()
        if not item:
            raise HTTPException(404, "Action request not found")
        item.status = data.status
        db.commit()
        return {"status": "updated"}
    except HTTPException:
        db.rollback()
        raise
    finally:
        db.close()
