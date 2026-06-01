from fastapi import APIRouter, HTTPException
from ..models.jellyfin import Genre
from ..services.jellyfin import jellyfin

router = APIRouter(prefix="/genres", tags=["genres"])


@router.get("", response_model=list[Genre])
async def list_genres():
    """Return all music genres available on the Jellyfin server."""
    try:
        return await jellyfin.get_genres()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
