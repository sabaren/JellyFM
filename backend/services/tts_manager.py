"""
TTS manager — priority chain: Kokoro (neural) > espeak (fallback) > nothing.
All callers should import from here rather than the individual services.
"""
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

    Called as a background task at server startup.  Because kokoro.synthesize
    holds _synthesis_lock, the first real TTS call from the broadcast loop will
    block on the lock rather than blocking on cold compilation itself — the
    result is that listeners hear audio much sooner after server boot.
    """
    if not kokoro.is_available():
        return
    logger.info("TTS warm-up: triggering Kokoro ONNX compilation…")
    try:
        await synthesize("Warming up.")
        logger.info("TTS warm-up complete.")
    except Exception:
        logger.warning("TTS warm-up failed (non-fatal)", exc_info=True)
