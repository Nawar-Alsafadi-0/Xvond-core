from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from backend.app.modules.media.generated_media import (
    MediaGenerationError,
    resolve_signed_media,
)


router = APIRouter(prefix="/media", tags=["Public Media"])


@router.get("/generated/{filename}")
def generated_media(
    filename: str,
    expires: int = Query(...),
    sig: str = Query(..., min_length=32, max_length=128),
):
    try:
        path = resolve_signed_media(filename, expires=expires, sig=sig)
    except MediaGenerationError as exc:
        raise HTTPException(404, "Generated media is unavailable") from exc

    return FileResponse(
        path,
        media_type="image/jpeg",
        filename=filename,
        headers={"Cache-Control": "private, max-age=300"},
    )
