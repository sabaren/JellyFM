"""
TTS manager — priority chain: Kokoro (neural) > espeak (fallback) > nothing.
All callers should import from here rather than the individual services.
"""
import asyncio
import io
import logging
from typing import Optional
from . import kokoro, espeak

logger = logging.getLogger(__name__)


def best_engine() -> str:
    if kokoro.is_available():
        return "kokoro"
    if espeak.is_available():
        return "espeak"
    return "none"


async def synthesize(text: str) -> Optional[bytes]:
    if kokoro.is_available():
        result = await kokoro.synthesize(text)
        if result:
            return result
    return await espeak.synthesize(text)


def is_available() -> bool:
    return kokoro.is_available() or espeak.is_available()


async def warmup() -> None:
    """Trigger ONNX JIT compilation before the first real announcement.

    Run in a thread executor so it doesn't block the event loop during startup.
    """
    if not kokoro.is_available():
        return
    logger.info("TTS warm-up: triggering Kokoro ONNX compilation…")
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _sync_warmup)
        logger.info("TTS warm-up complete.")
    except Exception:
        logger.warning("TTS warm-up failed (non-fatal)", exc_info=True)


def _sync_warmup() -> None:
    """Synchronous wrapper so run_in_executor actually runs off the main thread."""
    import soundfile as sf  # noqa: F401
    import onnxruntime as ort  # noqa: F401
    instance = kokoro._get_instance()
    if instance is None:
        return
    # Force a short synthesis to trigger ONNX graph compilation
    samples, sr = instance.create("Warm", voice="af_bella", speed=1.0, lang="en-us")
    buf = io.BytesIO()
    sf.write(buf, samples, sr, format="WAV", subtype="PCM_16")
