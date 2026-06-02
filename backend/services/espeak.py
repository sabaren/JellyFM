"""
Server-side TTS using espeak-ng.
Generates a WAV in memory and returns it as bytes so the browser can play
it as an audio blob before the music track starts.

Falls back gracefully if espeak is not installed — the API returns 204 and
the frontend skips the announcement.
"""
import asyncio
import logging
import shutil
from typing import Optional

logger = logging.getLogger(__name__)

# espeak-ng is preferred; fall back to espeak if available
_ESPEAK = shutil.which("espeak-ng") or shutil.which("espeak")


def is_available() -> bool:
    return _ESPEAK is not None


async def synthesize(text: str, rate: int = 150, pitch: int = 50) -> Optional[bytes]:
    """
    Synthesize *text* to WAV bytes using espeak.
    Returns None if espeak is unavailable or synthesis fails.
    """
    if not _ESPEAK:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            _ESPEAK,
            "--stdout",
            "-v", "en",
            "-s", str(rate),   # words per minute
            "-p", str(pitch),  # pitch 0-99
            text,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15.0)
        if proc.returncode == 0 and stdout:
            return stdout
    except asyncio.TimeoutError:
        logger.warning("espeak timed out for text: %r", text[:60])
    except Exception:
        logger.exception("espeak synthesis failed")
    return None
