"""
pyttsx3 wrapper that's safe to call from an asyncio context.

pyttsx3 is synchronous and not thread-safe across different engine instances,
so we keep one engine and run it in a dedicated thread via a single-threaded
ThreadPoolExecutor. All calls are serialised through that thread.
"""
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts")
_engine = None  # lazily initialised inside the TTS thread


def _get_engine():
    global _engine
    if _engine is None:
        import pyttsx3
        _engine = pyttsx3.init()
        _engine.setProperty("rate", 165)   # words per minute
        _engine.setProperty("volume", 0.9)
    return _engine


def _speak_sync(text: str) -> None:
    try:
        engine = _get_engine()
        engine.say(text)
        engine.runAndWait()
    except Exception:
        logger.exception("TTS error for text: %r", text)


async def speak(text: str) -> None:
    """Speak *text* asynchronously (non-blocking from the caller's perspective)."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(_executor, _speak_sync, text)


def build_track_announcement(
    artist: str,
    title: str,
    next_artist: Optional[str] = None,
    next_title: Optional[str] = None,
    banter: Optional[str] = None,
) -> str:
    parts: list[str] = []
    if banter:
        parts.append(banter)
    parts.append(f"Coming up: {title} by {artist}.")
    if next_title and next_artist:
        parts.append(f"And after that: {next_title} by {next_artist}.")
    return "  ".join(parts)
