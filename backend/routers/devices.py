from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from ..services import espeak

router = APIRouter(tags=["devices"])


@router.get("/tts/voices")
def list_tts_voices():
    """List available server-side espeak TTS voice presets."""
    return [
        {"id": pid, "label": label, "available": espeak.is_available()}
        for pid, label, _ in espeak.VOICE_PRESETS
    ]


class SetVoiceRequest(BaseModel):
    voice_id: str


@router.put("/tts/voice", status_code=204)
def set_tts_voice(body: SetVoiceRequest):
    if not espeak.set_voice(body.voice_id):
        raise HTTPException(status_code=400, detail=f"Unknown voice preset: {body.voice_id!r}")
