"""
Kokoro ONNX TTS service — studio-quality, fully offline voice synthesis.

Expected layout (see README for download instructions):
    ~/kokoro/kokoro-v0_19.onnx   ← ONNX model weights
    ~/kokoro/voices.json          ← voice embeddings

Dependencies: kokoro-onnx, soundfile, onnxruntime
"""
import asyncio
import io
import logging
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_MODEL_DIR   = Path.home() / "kokoro"
_MODEL_FILE  = _MODEL_DIR / "kokoro-v0_19.onnx"
_VOICES_FILE = _MODEL_DIR / "voices.json"

# Catalogue of built-in Kokoro voices
VOICES: dict[str, str] = {
    "af_bella":    "Bella (American Female)",
    "af_nicole":   "Nicole (American Female)",
    "af_sarah":    "Sarah (American Female)",
    "af_sky":      "Sky (American Female)",
    "am_adam":     "Adam (American Male)",
    "am_michael":  "Michael (American Male)",
    "bf_emma":     "Emma (British Female)",
    "bf_isabella": "Isabella (British Female)",
    "bm_george":   "George (British Male)",
    "bm_lewis":    "Lewis (British Male)",
}

_active_voice: str = "af_bella"
_instance = None
_init_lock = threading.Lock()


def _get_instance():
    global _instance
    if _instance is not None:
        return _instance
    if not _MODEL_FILE.exists() or not _VOICES_FILE.exists():
        return None
    with _init_lock:
        if _instance is not None:
            return _instance
        try:
            from kokoro_onnx import Kokoro  # type: ignore
            _instance = Kokoro(str(_MODEL_FILE), str(_VOICES_FILE))
            logger.info("Kokoro TTS ready — %s", _MODEL_FILE.name)
        except Exception:
            logger.exception("Kokoro TTS initialisation failed")
    return _instance


def is_available() -> bool:
    return _get_instance() is not None


def list_voices() -> list[dict]:
    if not is_available():
        return []
    return [{"id": vid, "label": label} for vid, label in VOICES.items()]


def set_voice(voice_id: str) -> bool:
    global _active_voice
    if voice_id in VOICES:
        _active_voice = voice_id
        return True
    return False


async def synthesize(text: str) -> Optional[bytes]:
    """Synthesize *text* to WAV bytes using the active Kokoro voice."""
    instance = _get_instance()
    if not instance:
        return None
    try:
        import soundfile as sf  # type: ignore

        voice   = _active_voice
        loop    = asyncio.get_running_loop()

        def _run() -> bytes:
            samples, sample_rate = instance.create(
                text, voice=voice, speed=1.05, lang="en-us"
            )
            buf = io.BytesIO()
            sf.write(buf, samples, sample_rate, format="WAV", subtype="PCM_16")
            return buf.getvalue()

        return await asyncio.wait_for(
            loop.run_in_executor(None, _run),
            timeout=30.0,
        )
    except asyncio.TimeoutError:
        logger.warning("Kokoro TTS timed out for: %r", text[:60])
    except Exception:
        logger.exception("Kokoro TTS synthesis failed")
    return None
