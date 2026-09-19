from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from backend.app.api.customer_management import (
    _invalidate_bound_integration_previews,
    _self_service_company,
    _store_validation_evidence,
)
from backend.app.core.config.settings import settings
from backend.app.core.config_secrets import reveal_config
from backend.app.core.database.connection import SessionLocal
from backend.app.core.dependencies import require_customer_manager
from backend.app.core.execution_claims import execution_claims
from backend.app.models.user import User
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.audit.service import audit_service
from backend.app.modules.billing.service_limits import service_limits
from backend.app.modules.integrations.catalog import validate_integration_config
from backend.app.modules.integrations.google_calendar import (
    CalendarConnectorError,
    validate_google_calendar_connection,
    validate_google_calendar_preferences,
)
from backend.app.modules.integrations.google_calendar_oauth import (
    GoogleCalendarOAuthError,
    build_google_calendar_authorization_url,
    exchange_google_calendar_code,
    google_calendar_oauth_ready,
    issue_google_calendar_oauth_state,
    verify_google_calendar_oauth_state,
)
from backend.app.modules.integrations.models import CompanyIntegration


router = APIRouter(
    prefix="/manage/integrations/google-calendar/oauth",
    tags=["Customer - Google Calendar OAuth"],
)


class GoogleCalendarOAuthStart(BaseModel):
    integration_id: int | None = None
    name: str | None = Field(default=None, max_length=200)
    calendar_id: str = Field(default="primary", max_length=1024)
    timezone: str | None = Field(default=None, max_length=100)
    slot_minutes: int = Field(default=30, ge=5, le=720)


def _portal_redirect(status: str) -> str:
    base = str(settings.PUBLIC_BASE_URL or "").strip().rstrip("/")
    return f"{base}/customer-ui?calendar_oauth={status}#integrations" if base else f"/customer-ui?calendar_oauth={status}#integrations"


def _manager_from_state(db, payload: dict) -> User:
    user = db.query(User).filter(User.id == int(payload.get("user_id") or 0)).first()
    company_id = int(payload.get("company_id") or 0)
    if (
        user is None
        or not user.active
        or user.role not in {"owner", "admin", "manager"}
        or int(user.company_id or 0) != company_id
    ):
        raise HTTPException(403, "Google Calendar connection session is no longer authorized")
    _self_service_company(db, user)
    return user


def _assert_reconnect_safe(db, *, company_id: int, integration_id: int) -> None:
    rows = (
        db.query(AIAgent)
        .filter(
            AIAgent.company_id == company_id,
            AIAgent.enabled.is_(True),
        )
        .all()
    )
    if not rows:
        return

    # The shared invalidation helper contains the authoritative binding lookup
    # and fails closed when the integration is used by a live employee. Roll
    # back the no-op invalidation immediately so starting OAuth never changes
    # preview evidence before consent is completed.
    try:
        _invalidate_bound_integration_previews(
            db,
            company_id=company_id,
            integration_id=integration_id,
        )
        db.rollback()
    except HTTPException:
        db.rollback()
        raise


@router.get("/status")
def google_calendar_oauth_status(
    current_user: User = Depends(require_customer_manager),
):
    return {
        "ready": google_calendar_oauth_ready(),
        "redirect_uri": (
            settings.GOOGLE_CALENDAR_OAUTH_REDIRECT_URI
            or (
                f"{str(settings.PUBLIC_BASE_URL).rstrip('/')}/manage/integrations/google-calendar/oauth/callback"
                if settings.PUBLIC_BASE_URL
                else None
            )
        ),
    }


@router.post("/start")
def google_calendar_oauth_start(
    payload: GoogleCalendarOAuthStart,
    current_user: User = Depends(require_customer_manager),
):
    if not google_calendar_oauth_ready():
        raise HTTPException(503, "Google Calendar OAuth is not configured")

    db = SessionLocal()
    try:
        company = _self_service_company(db, current_user)
        company_id = company.id

        if payload.integration_id is not None:
            item = (
                db.query(CompanyIntegration)
                .filter(
                    CompanyIntegration.id == payload.integration_id,
                    CompanyIntegration.company_id == company_id,
                    CompanyIntegration.integration_type == "calendar",
                )
                .first()
            )
            if item is None:
                raise HTTPException(404, "Google Calendar connection not found")
            _assert_reconnect_safe(
                db,
                company_id=company_id,
                integration_id=item.id,
            )
            existing = reveal_config(item.config) or {}
            name = item.name
            config = {
                "provider": "google",
                "calendar_id": existing.get("calendar_id") or "primary",
                "timezone": existing.get("timezone"),
                "slot_minutes": existing.get("slot_minutes") or 30,
            }
            integration_id = item.id
        else:
            name = str(payload.name or "").strip()
            if not name:
                raise HTTPException(400, "Connection name is required")
            config = {
                "provider": "google",
                "calendar_id": str(payload.calendar_id or "primary").strip(),
                "timezone": str(payload.timezone or "").strip(),
                "slot_minutes": payload.slot_minutes,
            }
            integration_id = None

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

        try:
            normalized = validate_google_calendar_preferences(config)
        except CalendarConnectorError as exc:
            raise HTTPException(400, str(exc)) from exc

        state, challenge = issue_google_calendar_oauth_state(
            user_id=current_user.id,
            company_id=company_id,
            integration_id=integration_id,
            name=name,
            config=normalized,
        )
        return {
            "authorization_url": build_google_calendar_authorization_url(
                state=state,
                code_challenge=challenge,
            ),
            "expires_in": 600,
        }
    finally:
        db.close()


@router.get("/callback")
def google_calendar_oauth_callback(
    state: str,
    code: str | None = None,
    error: str | None = None,
):
    try:
        oauth_state = verify_google_calendar_oauth_state(state)
    except GoogleCalendarOAuthError as exc:
        raise HTTPException(400, str(exc)) from exc

    if error:
        return RedirectResponse(_portal_redirect("cancelled"), status_code=303)
    if not str(code or "").strip():
        raise HTTPException(400, "Google OAuth authorization code is missing")

    nonce = str(oauth_state.get("nonce") or "").strip()
    claim_key = f"google_calendar_oauth:{nonce}"
    if not execution_claims.claim(claim_key, ttl_seconds=900):
        raise HTTPException(409, "Google Calendar connection callback was already used")

    try:
        token = exchange_google_calendar_code(
            code=str(code).strip(),
            code_verifier=str(oauth_state.get("code_verifier") or ""),
        )
    except GoogleCalendarOAuthError as exc:
        raise HTTPException(502, str(exc)) from exc

    db = SessionLocal()
    try:
        user = _manager_from_state(db, oauth_state)
        company_id = int(oauth_state["company_id"])
        integration_id = oauth_state.get("integration_id")
        config = dict(oauth_state.get("config") or {})

        if integration_id is not None:
            item = (
                db.query(CompanyIntegration)
                .filter(
                    CompanyIntegration.id == int(integration_id),
                    CompanyIntegration.company_id == company_id,
                    CompanyIntegration.integration_type == "calendar",
                )
                .with_for_update()
                .first()
            )
            if item is None:
                raise HTTPException(404, "Google Calendar connection not found")
            existing = reveal_config(item.config) or {}
            _invalidate_bound_integration_previews(
                db,
                company_id=company_id,
                integration_id=item.id,
            )
            refresh_token = token.get("refresh_token") or existing.get("refresh_token")
            config = {
                **existing,
                **config,
                "access_token": token["access_token"],
                "refresh_token": refresh_token,
            }
            config.pop("_xvond_validation", None)
        else:
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
            if not token.get("refresh_token"):
                raise HTTPException(
                    409,
                    "Google did not return offline access. Reconnect and grant Calendar access.",
                )
            item = CompanyIntegration(
                company_id=company_id,
                integration_type="calendar",
                name=str(oauth_state.get("name") or "Google Calendar")[:200],
                config={},
                enabled=True,
            )
            db.add(item)
            db.flush()
            config = {
                **config,
                "access_token": token["access_token"],
                "refresh_token": token["refresh_token"],
            }

        try:
            validate_integration_config("calendar", config)
            evidence = validate_google_calendar_connection(config, timeout=10.0)
        except (ValueError, CalendarConnectorError) as exc:
            raise HTTPException(409, str(exc)) from exc

        item.config = config
        _store_validation_evidence(item, evidence)
        audit_service.log(
            db=db,
            action="customer.google_calendar_connected",
            resource_type="integration",
            resource_id=item.id,
            user_id=user.id,
            company_id=company_id,
            details={
                "integration_type": "calendar",
                "validation_mode": evidence.get("mode"),
                "calendar_id": evidence.get("calendar_id"),
            },
        )
        db.commit()
        return RedirectResponse(_portal_redirect("connected"), status_code=303)
    except HTTPException:
        db.rollback()
        raise
    finally:
        db.close()
