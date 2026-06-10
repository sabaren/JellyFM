"""
TTS manager — priority chain: Kokoro (neural) > espeak (fallback) > nothing.
All callers should import from here rather than the individual services.
"""
from typing import Optional
from . import kokoro, espeak


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
