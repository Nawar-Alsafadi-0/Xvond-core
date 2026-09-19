from contextlib import asynccontextmanager
import logging
from pathlib import Path
from time import perf_counter

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from backend.app.api.auth import router as auth_router
from backend.app.api.users import router as users_router
from backend.app.api.admin import router as admin_router
from backend.app.api.admin_ai import router as admin_ai_router
from backend.app.api.admin_ai_employee import router as admin_ai_employee_router
from backend.app.api.admin_ai_employee_profile import router as admin_ai_employee_profile_router
from backend.app.api.admin_ai_employee_files import router as admin_ai_employee_files_router
from backend.app.api.admin_ai_employee_knowledge import router as admin_ai_employee_knowledge_router
from backend.app.api.admin_agent_actions import router as admin_agent_actions_router
from backend.app.api.admin_delivery_readiness import router as admin_delivery_readiness_router
from backend.app.api.admin_audit import router as admin_audit_router
from backend.app.api.admin_channels import router as admin_channels_router
from backend.app.api.admin_voice import router as admin_voice_router
from backend.app.api.admin_meta_whatsapp import router as admin_meta_whatsapp_router
from backend.app.api.admin_company_profile import router as admin_company_profile_router
from backend.app.api.admin_company_users import router as admin_company_users_router
from backend.app.api.admin_company_view import router as admin_company_view_router
from backend.app.api.admin_dashboard import router as admin_dashboard_router
from backend.app.api.admin_integrations import router as admin_integrations_router
from backend.app.api.admin_knowledge import router as admin_knowledge_router
from backend.app.api.admin_operations import router as admin_operations_router
from backend.app.api.admin_production import router as admin_production_router
from backend.app.api.admin_providers import router as admin_providers_router
from backend.app.api.admin_setup import router as admin_setup_router
from backend.app.api.admin_solutions import router as admin_solutions_router
from backend.app.api.admin_tool_execution import router as admin_tool_execution_router
from backend.app.api.admin_tools import router as admin_tools_router
from backend.app.api.admin_automation import router as admin_automation_router
from backend.app.api.admin_analytics_builder import router as admin_analytics_builder_router
from backend.app.api.admin_service_billing import router as admin_service_billing_router
from backend.app.api.internal_workflow_actions import router as internal_workflow_actions_router
from backend.app.api.internal_channel_gateway import router as internal_channel_gateway_router
from backend.app.api.public_channels import router as public_channels_router
from backend.app.api.public_employee_builder import router as public_employee_builder_router
from backend.app.api.public_billing import router as public_billing_router
from backend.app.api.public_media import router as public_media_router
from backend.app.api.public_automation_webhooks import router as public_automation_webhooks_router
from backend.app.api.voice_llm import router as voice_llm_router
from backend.app.api.website_widget import router as website_widget_router
from backend.app.api.ai_agents import router as ai_agents_router
from backend.app.api.company_modules import router as company_modules_router
from backend.app.api.customer_action_requests import router as customer_action_requests_router
from backend.app.api.customer_agents import router as customer_agents_router
from backend.app.api.customer_employee_builder import router as customer_employee_builder_router
from backend.app.api.customer_business import router as customer_business_router
from backend.app.api.customer_inbox import router as customer_inbox_router
from backend.app.api.customer_meta_whatsapp import router as customer_meta_whatsapp_router
from backend.app.api.customer_operations import router as customer_operations_router
from backend.app.api.customer_portal import router as customer_portal_router
from backend.app.api.customer_subscription import router as customer_subscription_router
from backend.app.api.billing_webhooks import router as billing_webhooks_router
from backend.app.api.modules import router as modules_router
from backend.app.api.usage import router as usage_router
from backend.app.api.whatsapp_webhook import router as whatsapp_webhook_router
from backend.app.core.admin_privacy import enforce_admin_customer_data_boundary
from backend.app.core.config.settings import settings
from backend.app.core.database.connection import SessionLocal
from backend.app.core.log_config import (
    configure_logging,
    reset_request_id,
    safe_request_id,
    set_request_id,
)
from backend.app.core.module_loader import discover_modules
from backend.app.core.module_manager import module_manager
from backend.app.core.rate_limit import rate_limiter

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = PROJECT_ROOT / "frontend"
CUSTOMER_PORTAL_VERSION = "20260917-selfservice1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    for module in discover_modules(strict=settings.is_production):
        module_manager.install(module)
        module_manager.enable(module.name)
    yield


configure_logging(level=settings.LOG_LEVEL, json_logs=settings.LOG_JSON)
logger = logging.getLogger("xvond.http")
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=[
        "Content-Type",
        "X-Xvond-Widget-Key",
        "X-Xvond-Visitor-Token",
    ],
)


def request_client_ip(request: Request) -> str:
    if settings.TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-for", "")
        proxy_ip = (
            forwarded.split(",", 1)[0].strip()
            or request.headers.get("cf-connecting-ip", "").strip()
        )
        if proxy_ip:
            return proxy_ip
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def request_observability(request: Request, call_next):
    request_id = safe_request_id(request.headers.get("x-request-id"))
    token = set_request_id(request_id)
    started = perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "Unhandled request failure",
            extra={
                "method": request.method,
                "path": request.url.path,
                "client_ip": request_client_ip(request),
            },
        )
        raise
    finally:
        reset_request_id(token)
    duration_ms = round((perf_counter() - started) * 1000, 2)
    response.headers["X-Request-ID"] = request_id
    if request.url.path.startswith(("/customer/", "/admin/", "/auth/", "/ai-agents/")) or request.url.path.endswith(".html") or request.url.path in {"/admin-ui", "/customer-ui", "/login", "/dashboard", "/build"}:
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    logger.info(
        "Request completed",
        extra={
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
            "client_ip": request_client_ip(request),
        },
    )
    return response


_RATE_LIMITS = {
    ("POST", "/auth/login"): (10, 60),
    ("POST", "/auth/signup"): (5, 300),
    ("POST", "/auth/customer/forgot-password"): (5, 300),
    ("POST", "/auth/customer/reset-password"): (10, 300),
    ("POST", "/public/employee-builder/preview"): (60, 60),
    ("POST", "/webhooks/whatsapp"): (240, 60),
    ("POST", "/webhooks/billing/paddle"): (240, 60),
    ("POST", "/webhooks/billing/tap"): (240, 60),
}


@app.middleware("http")
async def protect_public_endpoints(request: Request, call_next):
    rule = _RATE_LIMITS.get((request.method.upper(), request.url.path))
    if rule is None and request.method.upper() == "POST":
        if request.url.path.startswith("/ai-agents/") and request.url.path.endswith("/chat"):
            rule = (60, 60)
        elif request.url.path.startswith("/channels/website/") and request.url.path.endswith("/chat"):
            rule = (90, 60)
        elif request.url.path.startswith("/channels/voice/") and request.url.path.endswith("/turn"):
            rule = (180, 60)
        elif request.url.path.startswith("/v1/voice/") and request.url.path.endswith("/chat/completions"):
            rule = (240, 60)
        elif request.url.path.startswith("/webhooks/automation/"):
            rule = (120, 60)
    if rule is not None:
        client_ip = request_client_ip(request)
        limit, window = rule
        key = f"{client_ip}:{request.method}:{request.url.path}"
        if not rate_limiter.allow(key, limit, window):
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests"},
                headers={"Retry-After": str(window)},
            )
    return await call_next(request)


enforce_admin_customer_data_boundary(admin_agent_actions_router)


for r in [
    auth_router,
    users_router,
    admin_router,
    admin_ai_router,
    admin_ai_employee_router,
    admin_ai_employee_profile_router,
    admin_ai_employee_files_router,
    admin_ai_employee_knowledge_router,
    admin_agent_actions_router,
    admin_delivery_readiness_router,
    admin_audit_router,
    admin_channels_router,
    admin_voice_router,
    admin_meta_whatsapp_router,
    admin_company_profile_router,
    admin_company_users_router,
    admin_company_view_router,
    admin_dashboard_router,
    admin_integrations_router,
    admin_knowledge_router,
    admin_operations_router,
    admin_production_router,
    admin_providers_router,
    admin_setup_router,
    admin_solutions_router,
    admin_tool_execution_router,
    admin_tools_router,
    admin_automation_router,
    admin_analytics_builder_router,
    admin_service_billing_router,
    internal_workflow_actions_router,
    internal_channel_gateway_router,
    ai_agents_router,
    public_channels_router,
    public_employee_builder_router,
    public_billing_router,
    public_media_router,
    public_automation_webhooks_router,
    voice_llm_router,
    website_widget_router,
    modules_router,
    company_modules_router,
    customer_action_requests_router,
    customer_agents_router,
    customer_employee_builder_router,
    customer_business_router,
    customer_inbox_router,
    customer_meta_whatsapp_router,
    customer_operations_router,
    customer_portal_router,
    customer_subscription_router,
    billing_webhooks_router,
    usage_router,
    whatsapp_webhook_router,
]:
    app.include_router(r)

app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
def root():
    return {
        "name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "status": "online",
    }


@app.get("/health/live")
def liveness():
    return {"status": "alive", "version": settings.APP_VERSION}


@app.get("/health")
@app.get("/health/ready")
def health():
    db = SessionLocal()
    database_ok = False
    try:
        db.execute(text("SELECT 1"))
        database_ok = True
    except Exception:
        database_ok = False
    finally:
        db.close()
    redis_ok = rate_limiter.healthcheck()
    ready = database_ok and redis_ok
    payload = {
        "status": "healthy" if ready else "unhealthy",
        "database": "ok" if database_ok else "unavailable",
        "redis": "ok" if redis_ok else "unavailable",
        "environment": settings.APP_ENV,
        "version": settings.APP_VERSION,
    }
    if not ready:
        return JSONResponse(status_code=503, content=payload)
    return payload


@app.get("/admin-ui")
def admin_ui():
    return RedirectResponse(url=f"/static/admin/index.html?v={CUSTOMER_PORTAL_VERSION}")


@app.get("/customer-ui")
@app.get("/login")
@app.get("/dashboard")
def customer_ui():
    return RedirectResponse(
        url=f"/static/customer/index.html?v={CUSTOMER_PORTAL_VERSION}"
    )


@app.get("/build")
def public_employee_builder():
    return RedirectResponse(
        url=f"/static/public/employee-builder.html?v={CUSTOMER_PORTAL_VERSION}"
    )


@app.get("/checkout")
def checkout():
    return RedirectResponse(url=f"/static/public/checkout.html?v={CUSTOMER_PORTAL_VERSION}")


@app.get("/billing/return")
def billing_return():
    return RedirectResponse(url="/customer-ui#employee-builder")


@app.get("/privacy")
def privacy():
    return RedirectResponse(url="/static/privacy.html")
