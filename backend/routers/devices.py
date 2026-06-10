from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from ..services import kokoro, espeak, tts_manager


router = APIRouter(tags=["devices"])


@router.get("/tts/voices")
def list_tts_voices():
    """
    Returns available server-side TTS voices.
    Kokoro neural voices are listed first when present; espeak presets follow.
    """
    engine = tts_manager.best_engine()
    voices = []

    for v in kokoro.list_voices():
        voices.append({
            "id":     f"kokoro:{v['id']}",
            "label":  v["label"],
            "engine": "kokoro",
            "active": engine == "kokoro",
        })

    for pid, label, _ in espeak.VOICE_PRESETS:
        voices.append({
            "id":     f"espeak:{pid}",
            "label":  f"{label} (espeak)",
            "engine": "espeak",
            "active": engine == "espeak",
        })

    return {
        "engine":           engine,
        "kokoro_available": kokoro.is_available(),
        "espeak_available": espeak.is_available(),
        "voices":           voices,
    }


class SetVoiceRequest(BaseModel):
    voice_id: str  # "kokoro:af_bella" or "espeak:male-us"


@router.put("/tts/voice", status_code=204)
def set_tts_voice(body: SetVoiceRequest):
    vid = body.voice_id
    if vid.startswith("kokoro:"):
        voice_id = vid.removeprefix("kokoro:")
        if not kokoro.set_voice(voice_id):
            raise HTTPException(400, detail=f"Unknown Kokoro voice: {voice_id!r}")
    elif vid.startswith("espeak:"):
        preset = vid.removeprefix("espeak:")
        if not espeak.set_voice(preset):
            raise HTTPException(400, detail=f"Unknown espeak preset: {preset!r}")
    else:
        raise HTTPException(400, detail="voice_id must start with 'kokoro:' or 'espeak:'")
