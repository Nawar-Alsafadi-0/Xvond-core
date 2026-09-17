from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.app.modules.ai_agent.employee_builder import (
    blueprint_readiness,
    build_employee_blueprint,
)


router = APIRouter(
    prefix="/public/employee-builder",
    tags=["Public Employee Builder"],
)


class PublicEmployeePreviewRequest(BaseModel):
    description: str = Field(min_length=8, max_length=4000)


@router.post("/preview")
def preview_employee(data: PublicEmployeePreviewRequest):
    """Rule-based public preview. This endpoint does not call an AI provider."""
    try:
        blueprint = build_employee_blueprint(data.description)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "blueprint": blueprint.as_dict(),
        "readiness": blueprint_readiness(blueprint),
        "lifecycle": "preview",
        "ai_usage": False,
    }
