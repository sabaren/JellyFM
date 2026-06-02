from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from ..services import espeak, piper, tts_manager


router = APIRouter(tags=["devices"])


@router.get("/tts/voices")
def list_tts_voices():
    """
    Returns available server-side TTS voices.
    Piper models are listed first when present; espeak presets follow as fallback.
    """
    engine = tts_manager.best_engine()
    voices = []

    piper_models = piper.list_models()
    for m in piper_models:
        voices.append({
            "id": f"piper:{m['id']}",
            "label": m["label"],
            "engine": "piper",
            "active": engine == "piper",
        })

    for pid, label, _ in espeak.VOICE_PRESETS:
        voices.append({
            "id": f"espeak:{pid}",
            "label": f"{label} (espeak)",
            "engine": "espeak",
            "active": engine == "espeak",
        })

    return {
        "engine": engine,
        "piper_available": piper.is_available(),
        "espeak_available": espeak.is_available(),
        "voices": voices,
    }


class SetVoiceRequest(BaseModel):
    voice_id: str  # "piper:en_US-amy-medium.onnx" or "espeak:male-us"


@router.put("/tts/voice", status_code=204)
def set_tts_voice(body: SetVoiceRequest):
    vid = body.voice_id
    if vid.startswith("piper:"):
        model_file = vid.removeprefix("piper:")
        if not piper.set_model(model_file):
            raise HTTPException(400, detail=f"Piper model not found: {model_file!r}")
    elif vid.startswith("espeak:"):
        preset = vid.removeprefix("espeak:")
        if not espeak.set_voice(preset):
            raise HTTPException(400, detail=f"Unknown espeak preset: {preset!r}")
    else:
        raise HTTPException(400, detail="voice_id must start with 'piper:' or 'espeak:'")
