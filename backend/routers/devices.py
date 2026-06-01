from fastapi import APIRouter
from ..services.playback import playback_service

router = APIRouter(prefix="/devices", tags=["devices"])


@router.get("")
def list_audio_devices():
    """List all audio output devices visible to VLC on this host."""
    return playback_service.list_audio_devices()
