from fastapi import APIRouter

from backend.app.modules.channels.catalog import (
    CHANNEL_RUNTIME_LIVE,
    CHANNEL_SETUP_INTERNAL,
    CHANNEL_SETUP_MANAGED,
    CHANNEL_SETUP_SELF_SERVICE,
    list_customer_channel_capabilities,
)


router = APIRouter(
    prefix="/public/employee-builder",
    tags=["Public Employee Builder"],
)

# The public builder is intentionally UI-only until account creation.
# Building and saving a Draft goes through the authenticated customer create
# endpoint after login/signup. No public preview or AI endpoint is exposed.


@router.get("/channels")
def public_employee_builder_channels():
    """Return safe channel choices for the unauthenticated Build page."""

    channels = []
    for item in list_customer_channel_capabilities():
        setup_mode = item.get("setup_mode")
        runtime_state = item.get("runtime_state")
        if setup_mode == CHANNEL_SETUP_INTERNAL:
            availability = "built_in"
            setup_label = "Built in"
        elif runtime_state == CHANNEL_RUNTIME_LIVE and setup_mode == CHANNEL_SETUP_SELF_SERVICE:
            availability = "self_service_live"
            setup_label = "Connect yourself"
        elif runtime_state == CHANNEL_RUNTIME_LIVE and setup_mode == CHANNEL_SETUP_MANAGED:
            if item.get("packaged_provider") is True:
                availability = "xvond_managed_live"
                setup_label = "Xvond sets it up"
            else:
                availability = "xvond_managed_custom"
                setup_label = "Custom Xvond setup"
        else:
            availability = "xvond_managed_adapter"
            setup_label = "Xvond managed setup"

        channels.append(
            {
                "type": item["type"],
                "name": item.get("name") or item["type"],
                "description": item.get("description") or "",
                "setup_mode": setup_mode,
                "runtime_state": runtime_state,
                "availability": availability,
                "setup_label": setup_label,
                "packaged_provider": bool(item.get("packaged_provider")),
            }
        )

    return {"channels": channels}
