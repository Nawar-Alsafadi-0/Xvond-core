from fastapi import APIRouter


router = APIRouter(
    prefix="/public/employee-builder",
    tags=["Public Employee Builder"],
)

# The public builder is intentionally UI-only until account creation.
# Building and saving a Draft goes through the authenticated customer create
# endpoint after login/signup. No public preview or AI endpoint is exposed.
