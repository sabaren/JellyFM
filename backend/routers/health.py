from fastapi import APIRouter
from ..services.jellyfin import jellyfin

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    return {"status": "ok", "jellyfin_authenticated": jellyfin.is_authenticated}


@router.post("/auth")
async def authenticate():
    """Manually trigger Jellyfin authentication (useful for testing credentials)."""
    await jellyfin.authenticate()
    return {"status": "authenticated", "user_id": jellyfin._user_id}
