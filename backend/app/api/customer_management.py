from datetime import datetime
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from backend.app.api.admin_ai_employee_files import upload_pdf_knowledge
from backend.app.api.admin_ai_employee_knowledge import (
    KnowledgeCreate,
    KnowledgeUpdate,
    WebsiteKnowledgeCreate,
    create_employee_knowledge,
    delete_employee_knowledge,
    get_employee_knowledge,
    ingest_website_knowledge,
    list_employee_knowledge,
    toggle_employee_knowledge,
    update_employee_knowledge,
)
from backend.app.api.admin_company_profile import (
    CompanyProfileUpdate,
    get_company_profile,
    update_company_profile,
)
from backend.app.core.dependencies import require_customer_manager
from backend.app.core.config.settings import settings
from backend.app.core.config_secrets import (
    configured_secret_fields,
    merge_config,
    public_config,
    reveal_config,
)
from backend.app.core.database.connection import SessionLocal
from backend.app.core.http_security import safe_http_request, validate_public_http_url
from backend.app.models.user import User
from backend.app.models.company import Company
from backend.app.models.company_profile import CompanyProfile
from backend.app.modules.billing.service_limits import service_limits
from backend.app.modules.integrations.catalog import (
    get_integration_definition,
    integration_validation_ready,
    list_integration_definitions,
    validate_integration_config,
)
from backend.app.modules.integrations.models import CompanyIntegration
from backend.app.modules.integrations.email_smtp import (
    EmailConnectorError,
    validate_smtp_connection,
)
from backend.app.modules.integrations.email_imap import (
    EmailReadConnectorError,
    validate_imap_connection,
)
from backend.app.modules.integrations.google_calendar import (
    GoogleCalendarError,
    build_google_calendar_authorization_url,
    exchange_google_calendar_code,
    google_calendar_oauth_ready,
    validate_google_calendar_connection,
)
from backend.app.modules.integrations.oauth_state import (
    create_integration_oauth_state,
    decode_integration_oauth_state,
)
from backend.app.core.execution_claims import execution_claims
from backend.app.modules.ai_agent.factory_models import AgentConfig
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.audit.service import audit_service
from backend.app.modules.tools.models import AgentToolAssignment

router = APIRouter(prefix="/manage", tags=["Customer Manager Controls"])


class CustomerIntegrationCreate(BaseModel):
    integration_type: str
    name: str = Field(min_length=1, max_length=200)
    config: dict = Field(default_factory=dict)


class CustomerIntegrationUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    config: dict | None = None
    enabled: bool | None = None


class GoogleCalendarOAuthStartRequest(BaseModel):
    name: str = Field(default="Google Calendar", min_length=1, max_length=200)
    calendar_id: str = Field(default="primary", min_length=1, max_length=500)


def _validate_live_connection(item: CompanyIntegration) -> dict:
    config = reveal_config(item.config) or {}
    integration_type = str(item.integration_type or "").strip().lower()

    if integration_type == "webhook":
        url = validate_public_http_url(str(config.get("url") or "").strip())
        return {
            "validated": True,
            "mode": "safe_url_validation",
            "url": url,
        }

    if integration_type == "email_imap":
        try:
            return validate_imap_connection(config, timeout=10.0)
        except EmailReadConnectorError as exc:
            raise HTTPException(409, str(exc)) from exc

    if integration_type == "email_smtp":
        try:
            return validate_smtp_connection(config, timeout=10.0)
        except EmailConnectorError as exc:
            raise HTTPException(409, str(exc)) from exc

    if integration_type == "google_calendar":
        try:
            evidence, token_updates = validate_google_calendar_connection(config)
        except GoogleCalendarError as exc:
            raise HTTPException(409, str(exc)) from exc
        if token_updates:
            item.config = {**config, **token_updates}
        return evidence

    if integration_type == "instagram_publish":
        instagram_user_id = str(config.get("instagram_user_id") or "").strip()
        access_token = str(config.get("access_token") or "").strip()
        if not instagram_user_id or not access_token:
            raise HTTPException(
                409,
                "Instagram User ID and access token are required before validation",
            )
        url = validate_public_http_url(
            f"https://graph.facebook.com/{settings.META_GRAPH_API_VERSION}/{instagram_user_id}?fields=id,username"
        )
        try:
            result = safe_http_request(
                url=url,
                method="GET",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {access_token}",
                },
                timeout=10,
                max_response_bytes=64_000,
            )
        except Exception as exc:
            raise HTTPException(502, "Instagram connection validation request failed") from exc
        status = int(result.get("status_code") or 0)
        if not 200 <= status < 300:
            raise HTTPException(
                409,
                f"Instagram connection validation returned HTTP {status}",
            )
        return {
            "validated": True,
            "mode": "instagram_live_read_only_request",
            "status_code": status,
        }

    if integration_type not in {"custom_api", "pos", "crm", "erp"}:
        raise HTTPException(
            409,
            "This connected-system type requires a packaged Xvond connector before it can be validated for execution",
        )

    base_url = validate_public_http_url(
        str(config.get("base_url") or "").strip().rstrip("/")
    )
    endpoint = str(config.get("validation_endpoint") or "").strip()
    if not endpoint:
        raise HTTPException(
            409,
            "Validation endpoint is required before this system can be used by an AI employee",
        )
    if endpoint.startswith("//") or endpoint.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "Validation endpoint must be a relative path")

    headers = {"Accept": "application/json"}
    api_key = str(config.get("api_key") or "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        result = safe_http_request(
            url=base_url + "/" + endpoint.lstrip("/"),
            method="GET",
            headers=headers,
            timeout=10,
            max_response_bytes=64_000,
        )
    except Exception as exc:
        raise HTTPException(502, "Connected system validation request failed") from exc

    status = int(result.get("status_code") or 0)
    if not 200 <= status < 300:
        raise HTTPException(
            409,
            f"Connected system validation returned HTTP {status}",
        )
    return {
        "validated": True,
        "mode": "live_read_only_request",
        "status_code": status,
        "endpoint": "/" + endpoint.lstrip("/"),
    }


def _store_validation_evidence(item: CompanyIntegration, evidence: dict) -> None:
    plain = reveal_config(item.config) or {}
    plain["_xvond_validation"] = {
        **dict(evidence or {}),
        "validated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    item.config = plain


def _spec_uses_integration(spec: dict | None, integration_id: int) -> bool:
    if not isinstance(spec, dict):
        return False
    for requirement in spec.get("requirements") or []:
        if not isinstance(requirement, dict):
            continue
        try:
            bound_id = int(requirement.get("integration_id") or 0)
        except (TypeError, ValueError):
            bound_id = 0
        if bound_id == int(integration_id):
            return True
    return False


def _runtime_assignment_agent_ids(
    db,
    *,
    company_id: int,
    integration_id: int,
) -> list[int]:
    agent_ids: list[int] = []
    rows = (
        db.query(AgentToolAssignment)
        .join(AIAgent, AIAgent.id == AgentToolAssignment.agent_id)
        .filter(AIAgent.company_id == company_id)
        .all()
    )
    for row in rows:
        config = reveal_config(row.config) or {}
        actions = config.get("actions") if isinstance(config, dict) else {}
        if not isinstance(actions, dict):
            continue
        for action in actions.values():
            if not isinstance(action, dict):
                continue
            destination = action.get("destination")
            if not isinstance(destination, dict):
                continue
            try:
                bound_id = int(destination.get("integration_id") or 0)
            except (TypeError, ValueError):
                bound_id = 0
            if bound_id == int(integration_id) and row.agent_id not in agent_ids:
                agent_ids.append(row.agent_id)
    return agent_ids


def _integration_binding_agent_ids(
    db,
    *,
    company_id: int,
    integration_id: int,
) -> tuple[list[int], list[int]]:
    """Return (live/current binding ids, pending-revision binding ids)."""
    live_ids = _runtime_assignment_agent_ids(
        db,
        company_id=company_id,
        integration_id=integration_id,
    )
    pending_ids: list[int] = []

    configs = (
        db.query(AgentConfig)
        .join(AIAgent, AIAgent.id == AgentConfig.agent_id)
        .filter(AIAgent.company_id == company_id)
        .all()
    )
    for config in configs:
        settings_value = config.settings if isinstance(config.settings, dict) else {}
        builder = settings_value.get("employee_builder")
        if not isinstance(builder, dict):
            continue
        if (
            _spec_uses_integration(builder.get("compiled_spec"), integration_id)
            and config.agent_id not in live_ids
        ):
            live_ids.append(config.agent_id)

        pending = builder.get("pending_revision")
        pending_spec = (
            pending.get("compiled_spec")
            if isinstance(pending, dict)
            else None
        )
        if (
            _spec_uses_integration(pending_spec, integration_id)
            and config.agent_id not in pending_ids
        ):
            pending_ids.append(config.agent_id)

    return live_ids, pending_ids


def _integration_bound_agent_ids(
    db,
    *,
    company_id: int,
    integration_id: int,
) -> list[int]:
    live_ids, pending_ids = _integration_binding_agent_ids(
        db,
        company_id=company_id,
        integration_id=integration_id,
    )
    return list(dict.fromkeys([*live_ids, *pending_ids]))


def _integration_bound(db, *, company_id: int, integration_id: int) -> bool:
    return bool(
        _integration_bound_agent_ids(
            db,
            company_id=company_id,
            integration_id=integration_id,
        )
    )


def _invalidate_bound_integration_previews(
    db,
    *,
    company_id: int,
    integration_id: int,
) -> int:
    """Invalidate only the build evidence that depends on a changed connection."""

    live_ids, pending_ids = _integration_binding_agent_ids(
        db,
        company_id=company_id,
        integration_id=integration_id,
    )
    all_ids = list(dict.fromkeys([*live_ids, *pending_ids]))
    if not all_ids:
        return 0

    if live_ids:
        live_agents = (
            db.query(AIAgent)
            .filter(
                AIAgent.company_id == company_id,
                AIAgent.id.in_(live_ids),
            )
            .all()
        )
        if any(agent.enabled for agent in live_agents):
            raise HTTPException(
                409,
                "This connected system is used by the live employee. Stage a revision that removes or replaces the connection before changing it.",
            )

    changed = 0
    configs = db.query(AgentConfig).filter(AgentConfig.agent_id.in_(all_ids)).all()
    for config in configs:
        settings_value = dict(config.settings or {})
        stored_builder = settings_value.get("employee_builder")
        if not isinstance(stored_builder, dict):
            continue
        builder = dict(stored_builder)

        if config.agent_id in live_ids:
            had_live_evidence = bool(
                builder.get("last_tested_at")
                or builder.get("last_tested_compiled_at")
            )
            builder.pop("last_tested_at", None)
            builder.pop("last_tested_compiled_at", None)
            if had_live_evidence:
                changed += 1

        if config.agent_id in pending_ids:
            pending = builder.get("pending_revision")
            if isinstance(pending, dict):
                pending = dict(pending)
                had_pending_evidence = bool(
                    pending.get("last_tested_at")
                    or pending.get("last_tested_compiled_at")
                )
                pending.pop("last_tested_at", None)
                pending.pop("last_tested_compiled_at", None)
                if isinstance(pending.get("compiled_spec"), dict):
                    pending["status"] = "built"
                builder["pending_revision"] = pending
                if had_pending_evidence:
                    changed += 1

        settings_value["employee_builder"] = builder
        config.settings = settings_value

    return changed



def _serialize_integration(item: CompanyIntegration) -> dict:
    definition = get_integration_definition(item.integration_type) or {}
    try:
        configured = validate_integration_config(
            item.integration_type,
            reveal_config(item.config),
        ) is True
    except ValueError:
        configured = False
    plain = reveal_config(item.config) or {}
    return {
        "id": item.id,
        "integration_type": item.integration_type,
        "name": item.name,
        "execution_adapter": definition.get("execution_adapter"),
        "requirement_keys": list(definition.get("requirement_keys") or []),
        "generic_requirements": bool(definition.get("generic_requirements") is True),
        "operation_endpoints": bool(definition.get("operation_endpoints") is True),
        "connection_mode": definition.get("connection_mode") or "config",
        "oauth_provider": definition.get("oauth_provider"),
        "oauth_status": (
            plain.get("_xvond_oauth_status")
            if definition.get("connection_mode") == "oauth"
            else None
        ),
        "config": public_config(item.config),
        "configured_secret_fields": configured_secret_fields(item.config),
        "configured": configured,
        "validated": integration_validation_ready(plain),
        "validated_at": (
            (plain.get("_xvond_validation") or {}).get("validated_at")
            if isinstance(plain.get("_xvond_validation"), dict)
            else None
        ),
        "enabled": bool(item.enabled),
        "created_at": item.created_at,
    }


def _company_id(user: User) -> int:
    if user.company_id is None:
        raise HTTPException(403, "Customer company required")
    return user.company_id


def _self_service_company(db, user: User) -> Company:
    company_id = _company_id(user)
    company = db.query(Company).filter(Company.id == company_id).first()
    if company is None:
        raise HTTPException(404, "Company not found")
    if str(company.onboarding_source or "").strip().lower() != "self_service":
        raise HTTPException(
            409,
            "Connected systems for Xvond Managed customers are configured by Xvond",
        )
    return company


@router.get("/business-information")
def customer_business_information(
    current_user: User = Depends(require_customer_manager),
):
    return get_company_profile(
        company_id=_company_id(current_user),
        current_admin=current_user,
    )


@router.put("/business-information")
def update_customer_business_information(
    payload: CompanyProfileUpdate,
    current_user: User = Depends(require_customer_manager),
):
    return update_company_profile(
        company_id=_company_id(current_user),
        data=payload,
        current_admin=current_user,
    )


@router.get("/{agent_id}/knowledge")
def customer_knowledge_list(
    agent_id: int,
    current_user: User = Depends(require_customer_manager),
):
    return list_employee_knowledge(
        company_id=_company_id(current_user),
        agent_id=agent_id,
        current_admin=current_user,
    )


@router.get("/{agent_id}/knowledge/{document_id}")
def customer_knowledge_item(
    agent_id: int,
    document_id: int,
    current_user: User = Depends(require_customer_manager),
):
    return get_employee_knowledge(
        company_id=_company_id(current_user),
        agent_id=agent_id,
        document_id=document_id,
        current_admin=current_user,
    )


@router.post("/{agent_id}/knowledge")
def customer_knowledge_create(
    agent_id: int,
    payload: KnowledgeCreate,
    current_user: User = Depends(require_customer_manager),
):
    return create_employee_knowledge(
        company_id=_company_id(current_user),
        agent_id=agent_id,
        payload=payload,
        current_admin=current_user,
    )


@router.post("/{agent_id}/knowledge/url")
def customer_knowledge_url(
    agent_id: int,
    payload: WebsiteKnowledgeCreate,
    current_user: User = Depends(require_customer_manager),
):
    return ingest_website_knowledge(
        company_id=_company_id(current_user),
        agent_id=agent_id,
        payload=payload,
        current_admin=current_user,
    )


@router.post("/{agent_id}/knowledge/pdf")
async def customer_knowledge_pdf(
    agent_id: int,
    file: UploadFile = File(...),
    current_user: User = Depends(require_customer_manager),
):
    return await upload_pdf_knowledge(
        company_id=_company_id(current_user),
        agent_id=agent_id,
        file=file,
        current_admin=current_user,
    )


@router.put("/{agent_id}/knowledge/{document_id}")
def customer_knowledge_update(
    agent_id: int,
    document_id: int,
    payload: KnowledgeUpdate,
    current_user: User = Depends(require_customer_manager),
):
    return update_employee_knowledge(
        company_id=_company_id(current_user),
        agent_id=agent_id,
        document_id=document_id,
        payload=payload,
        current_admin=current_user,
    )


@router.patch("/{agent_id}/knowledge/{document_id}/toggle")
def customer_knowledge_toggle(
    agent_id: int,
    document_id: int,
    current_user: User = Depends(require_customer_manager),
):
    return toggle_employee_knowledge(
        company_id=_company_id(current_user),
        agent_id=agent_id,
        document_id=document_id,
        current_admin=current_user,
    )


@router.delete("/{agent_id}/knowledge/{document_id}")
def customer_knowledge_delete(
    agent_id: int,
    document_id: int,
    current_user: User = Depends(require_customer_manager),
):
    return delete_employee_knowledge(
        company_id=_company_id(current_user),
        agent_id=agent_id,
        document_id=document_id,
        current_admin=current_user,
    )


@router.get("/integrations/catalog")
def customer_integration_catalog(
    current_user: User = Depends(require_customer_manager),
):
    _company_id(current_user)
    return {"integrations": list_integration_definitions()}


@router.post("/integrations/google-calendar/oauth/start")
def customer_google_calendar_oauth_start(
    payload: GoogleCalendarOAuthStartRequest,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        company = _self_service_company(db, current_user)
        if current_user.id is None:
            raise HTTPException(403, "Authenticated customer user is required")
        if not google_calendar_oauth_ready():
            raise HTTPException(
                409,
                "Google Calendar OAuth is not configured on Xvond yet",
            )

        current = (
            db.query(CompanyIntegration)
            .filter(CompanyIntegration.company_id == company.id)
            .count()
        )
        service_limits.check_current(
            db,
            company.id,
            "ai_agents",
            "integrations",
            current,
        )

        profile = (
            db.query(CompanyProfile)
            .filter(CompanyProfile.company_id == company.id)
            .first()
        )
        timezone = str(profile.timezone or "").strip() if profile else ""
        if not timezone:
            raise HTTPException(
                409,
                "Set the company timezone before connecting Google Calendar",
            )

        calendar_id = str(payload.calendar_id or "primary").strip() or "primary"
        item = CompanyIntegration(
            company_id=company.id,
            integration_type="google_calendar",
            name=str(payload.name or "Google Calendar").strip(),
            config={
                "calendar_id": calendar_id,
                "timezone": timezone,
                "_xvond_oauth_status": "pending",
            },
            enabled=False,
        )
        db.add(item)
        db.flush()

        state = create_integration_oauth_state(
            company_id=company.id,
            user_id=current_user.id,
            provider="google_calendar",
            integration_id=item.id,
        )
        state_payload = decode_integration_oauth_state(state)
        config = reveal_config(item.config) or {}
        config["_xvond_oauth_state_jti"] = state_payload["jti"]
        item.config = config

        audit_service.log(
            db=db,
            action="customer.integration_oauth_started",
            resource_type="integration",
            resource_id=item.id,
            user_id=current_user.id,
            company_id=company.id,
            details={"integration_type": "google_calendar"},
        )
        db.commit()
        return {
            "status": "authorization_required",
            "integration_id": item.id,
            "authorization_url": build_google_calendar_authorization_url(
                state=state
            ),
        }
    except HTTPException:
        db.rollback()
        raise
    except GoogleCalendarError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    finally:
        db.close()


@router.get("/integrations/google-calendar/oauth/callback")
def customer_google_calendar_oauth_callback(
    state: str = "",
    code: str = "",
    error: str = "",
):
    redirect_url = "/customer-ui#integrations"
    if error:
        return RedirectResponse(url=redirect_url, status_code=303)

    try:
        state_payload = decode_integration_oauth_state(state)
    except Exception:
        return RedirectResponse(url=redirect_url, status_code=303)
    if state_payload.get("provider") != "google_calendar":
        return RedirectResponse(url=redirect_url, status_code=303)

    claim_key = f"integration_oauth_state:{state_payload['jti']}"
    if not execution_claims.claim(claim_key, ttl_seconds=900):
        return RedirectResponse(url=redirect_url, status_code=303)

    db = SessionLocal()
    try:
        integration_id = int(state_payload.get("integration_id") or 0)
        item = (
            db.query(CompanyIntegration)
            .filter(
                CompanyIntegration.id == integration_id,
                CompanyIntegration.company_id == state_payload["company_id"],
                CompanyIntegration.integration_type == "google_calendar",
            )
            .with_for_update()
            .first()
        )
        if item is None:
            raise GoogleCalendarError("Google Calendar connection no longer exists")

        user = (
            db.query(User)
            .filter(
                User.id == state_payload["user_id"],
                User.company_id == state_payload["company_id"],
            )
            .first()
        )
        if user is None:
            raise GoogleCalendarError("Google Calendar connection user is invalid")

        config = reveal_config(item.config) or {}
        if str(config.get("_xvond_oauth_state_jti") or "") != state_payload["jti"]:
            raise GoogleCalendarError("Google Calendar OAuth state is stale")

        tokens = exchange_google_calendar_code(code)
        config.update(tokens)
        config["_xvond_oauth_status"] = "connected"
        config.pop("_xvond_oauth_state_jti", None)

        evidence, token_updates = validate_google_calendar_connection(config)
        config.update(token_updates)
        config["_xvond_validation"] = {
            **evidence,
            "validated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        }
        item.config = config
        item.enabled = True

        audit_service.log(
            db=db,
            action="customer.integration_oauth_connected",
            resource_type="integration",
            resource_id=item.id,
            user_id=user.id,
            company_id=item.company_id,
            details={
                "integration_type": "google_calendar",
                "validation_mode": evidence.get("mode"),
            },
        )
        db.commit()
        return RedirectResponse(url=redirect_url, status_code=303)
    except Exception:
        db.rollback()
        execution_claims.release(claim_key)
        return RedirectResponse(url=redirect_url, status_code=303)
    finally:
        db.close()


@router.get("/integrations")
def customer_integrations(
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        company_id = _company_id(current_user)
        rows = (
            db.query(CompanyIntegration)
            .filter(CompanyIntegration.company_id == company_id)
            .order_by(CompanyIntegration.id.asc())
            .all()
        )
        return {"integrations": [_serialize_integration(item) for item in rows]}
    finally:
        db.close()


@router.post("/integrations/{integration_id}/validate")
def customer_integration_validate(
    integration_id: int,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        company_id = _self_service_company(db, current_user).id
        item = (
            db.query(CompanyIntegration)
            .filter(
                CompanyIntegration.id == integration_id,
                CompanyIntegration.company_id == company_id,
                CompanyIntegration.enabled.is_(True),
            )
            .first()
        )
        if item is None:
            raise HTTPException(404, "Connected system not found or disabled")
        evidence = _validate_live_connection(item)
        _store_validation_evidence(item, evidence)
        audit_service.log(
            db=db,
            action="customer.integration_validated",
            resource_type="integration",
            resource_id=item.id,
            user_id=current_user.id,
            company_id=company_id,
            details={
                "integration_type": item.integration_type,
                "validation_mode": evidence.get("mode"),
                "status_code": evidence.get("status_code"),
            },
        )
        db.commit()
        return {
            "status": "validated",
            "integration_id": item.id,
            **evidence,
        }
    except HTTPException:
        db.rollback()
        raise
    finally:
        db.close()


@router.post("/integrations")
def customer_integration_create(
    payload: CustomerIntegrationCreate,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        company = _self_service_company(db, current_user)
        company_id = company.id
        integration_type = str(payload.integration_type or "").strip().lower()
        definition = get_integration_definition(integration_type)
        if definition is None:
            raise HTTPException(400, "Unsupported integration type")
        if definition.get("connection_mode") == "oauth":
            raise HTTPException(
                409,
                "Use the OAuth connection flow for this integration type",
            )
        name = str(payload.name or "").strip()
        if not name:
            raise HTTPException(400, "Integration name is required")
        try:
            validate_integration_config(integration_type, payload.config or {})
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        # The AI Employee plan may include integration limits. If there is no
        # integration entitlement configured yet, the existing service layer
        # will fail closed in production rather than silently exceeding a plan.
        try:
            current = (
                db.query(CompanyIntegration)
                .filter(
                    CompanyIntegration.company_id == company_id,
                    CompanyIntegration.enabled.is_(True),
                )
                .count()
            )
            service_limits.check_current(
                db,
                company_id,
                "ai_agents",
                "integrations",
                current,
            )
        except HTTPException:
            raise

        item = CompanyIntegration(
            company_id=company_id,
            integration_type=integration_type,
            name=name,
            config=payload.config or {},
            enabled=True,
        )
        db.add(item)
        db.flush()
        audit_service.log(
            db=db,
            action="customer.integration_created",
            resource_type="integration",
            resource_id=item.id,
            user_id=current_user.id,
            company_id=company_id,
            details={
                "integration_type": integration_type,
                "name": name,
                "configured_secret_fields": configured_secret_fields(item.config),
            },
        )
        db.commit()
        db.refresh(item)
        return {"status": "created", **_serialize_integration(item)}
    except HTTPException:
        db.rollback()
        raise
    finally:
        db.close()


@router.patch("/integrations/{integration_id}")
def customer_integration_update(
    integration_id: int,
    payload: CustomerIntegrationUpdate,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        company_id = _self_service_company(db, current_user).id
        item = (
            db.query(CompanyIntegration)
            .filter(
                CompanyIntegration.id == integration_id,
                CompanyIntegration.company_id == company_id,
            )
            .first()
        )
        if item is None:
            raise HTTPException(404, "Connected system not found")

        if payload.name is not None:
            name = str(payload.name or "").strip()
            if not name:
                raise HTTPException(400, "Integration name cannot be empty")
            item.name = name

        if payload.config is not None:
            merged = merge_config(item.config, payload.config)
            try:
                validate_integration_config(
                    item.integration_type,
                    reveal_config(merged),
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            invalidated_previews = _invalidate_bound_integration_previews(
                db,
                company_id=company_id,
                integration_id=item.id,
            )
            merged_plain = reveal_config(merged) or {}
            merged_plain.pop("_xvond_validation", None)
            item.config = merged_plain
        else:
            invalidated_previews = 0

        if payload.enabled is not None:
            if (
                payload.enabled is False
                and item.enabled
                and _integration_bound(
                    db,
                    company_id=company_id,
                    integration_id=item.id,
                )
            ):
                raise HTTPException(
                    409,
                    "This connected system is used by an AI employee. Change the employee setup before disabling it.",
                )
            item.enabled = bool(payload.enabled)

        audit_service.log(
            db=db,
            action="customer.integration_updated",
            resource_type="integration",
            resource_id=item.id,
            user_id=current_user.id,
            company_id=company_id,
            details={
                "integration_type": item.integration_type,
                "changed_fields": sorted(payload.model_dump(exclude_unset=True)),
                "configured_secret_fields": configured_secret_fields(item.config),
                "invalidated_employee_previews": invalidated_previews,
            },
        )
        db.commit()
        db.refresh(item)
        return {"status": "updated", **_serialize_integration(item)}
    except HTTPException:
        db.rollback()
        raise
    finally:
        db.close()


@router.delete("/integrations/{integration_id}")
def customer_integration_delete(
    integration_id: int,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        company_id = _self_service_company(db, current_user).id
        item = (
            db.query(CompanyIntegration)
            .filter(
                CompanyIntegration.id == integration_id,
                CompanyIntegration.company_id == company_id,
            )
            .first()
        )
        if item is None:
            raise HTTPException(404, "Connected system not found")
        if _integration_bound(
            db,
            company_id=company_id,
            integration_id=item.id,
        ):
            raise HTTPException(
                409,
                "This connected system is used by an AI employee. Change the employee setup before removing it.",
            )
        audit_service.log(
            db=db,
            action="customer.integration_deleted",
            resource_type="integration",
            resource_id=item.id,
            user_id=current_user.id,
            company_id=company_id,
            details={
                "integration_type": item.integration_type,
                "name": item.name,
            },
        )
        db.delete(item)
        db.commit()
        return {"status": "deleted", "integration_id": integration_id}
    finally:
        db.close()
