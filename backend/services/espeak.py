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

_ESPEAK = shutil.which("espeak-ng") or shutil.which("espeak")

# Voice presets — (id, label, espeak_voice_string)
VOICE_PRESETS = [
    ("male-us",     "Male — US",        "en-us+m3"),
    ("female-us",   "Female — US",      "en-us+f3"),
    ("male-uk",     "Male — UK",        "en+m3"),
    ("female-uk",   "Female — UK",      "en+f3"),
    ("male-warm",   "Male — Warm",      "en-us+m5"),
    ("female-soft", "Female — Soft",    "en-us+f1"),
]

# Active voice — can be changed at runtime via the API
_active_voice: str = VOICE_PRESETS[0][2]  # default: male US


def set_voice(preset_id: str) -> bool:
    global _active_voice
    for pid, _, vstr in VOICE_PRESETS:
        if pid == preset_id:
            _active_voice = vstr
            return True
    return False


def is_available() -> bool:
    return _ESPEAK is not None


async def synthesize(text: str, rate: int = 150, pitch: int = 50) -> Optional[bytes]:
    if not _ESPEAK:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            _ESPEAK,
            "--stdout",
            "-v", _active_voice,
            "-s", str(rate),
            "-p", str(pitch),
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
