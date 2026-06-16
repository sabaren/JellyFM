from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from ..services import kokoro, espeak, tts_manager


router = APIRouter(tags=["devices"])


@router.get("/tts/voices")
def list_tts_voices():
    """
    Returns available server-side TTS voices.
    Kokoro neural voices (base + blends) are listed first under engine="piper"
    so the frontend's existing optgroup filter continues to work.
    espeak presets follow as fallback.
    """
    engine = tts_manager.best_engine()
    voices = []

    for v in kokoro.list_voices():
        voices.append({
            "id":     f"piper:{v['id']}",   # "piper:" prefix for legacy filter
            "label":  v["label"],
            "engine": "piper",              # matches frontend v.engine === 'piper'
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
        "piper_available":  kokoro.is_available(),   # legacy key name
        "espeak_available": espeak.is_available(),
        "voices":           voices,
    }


class SetVoiceRequest(BaseModel):
    voice_id: str  # "piper:af_bella", "piper:blend_smooth_host", or "espeak:male-us"


@router.put("/tts/voice", status_code=204)
def set_tts_voice(body: SetVoiceRequest):
    vid = body.voice_id
    if vid.startswith("piper:"):
        # "piper:" is our compatibility prefix — routes to Kokoro underneath
        voice_id = vid.removeprefix("piper:")
        if not kokoro.set_voice(voice_id):
            raise HTTPException(400, detail=f"Unknown voice: {voice_id!r}")
    elif vid.startswith("kokoro:"):
        # Direct kokoro: prefix also accepted
        voice_id = vid.removeprefix("kokoro:")
        if not kokoro.set_voice(voice_id):
            raise HTTPException(400, detail=f"Unknown Kokoro voice: {voice_id!r}")
    elif vid.startswith("espeak:"):
        preset = vid.removeprefix("espeak:")
        if not espeak.set_voice(preset):
            raise HTTPException(400, detail=f"Unknown espeak preset: {preset!r}")
    else:
        raise HTTPException(
            400, detail="voice_id must start with 'piper:', 'kokoro:', or 'espeak:'"
        )
