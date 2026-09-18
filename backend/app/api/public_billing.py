from fastapi import APIRouter

from backend.app.core.config.settings import settings


router = APIRouter(prefix="/public/billing", tags=["Public Billing"])


@router.get("/config")
def public_billing_config():
    if settings.BILLING_PROVIDER != "paddle":
        return {"enabled": False, "provider": None}

    enabled = bool(
        settings.PADDLE_API_KEY
        and settings.PADDLE_WEBHOOK_SECRET
        and settings.PADDLE_CLIENT_TOKEN
        and settings.PADDLE_CHECKOUT_URL
    )
    if not enabled:
        return {"enabled": False, "provider": "paddle"}

    return {
        "enabled": True,
        "provider": "paddle",
        "environment": settings.PADDLE_ENVIRONMENT,
        "client_token": settings.PADDLE_CLIENT_TOKEN,
        "success_url": f"{settings.PUBLIC_BASE_URL}/customer-ui#employee-builder"
        if settings.PUBLIC_BASE_URL
        else "/customer-ui#employee-builder",
    }
