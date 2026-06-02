"""
TTS manager — picks the best available engine: Piper > espeak > nothing.
All callers should import from here rather than the individual services.
"""
from typing import Optional
from . import piper, espeak


def best_engine() -> str:
    if piper.is_available():
        return "piper"
    if espeak.is_available():
        return "espeak"
    return "none"


async def synthesize(text: str) -> Optional[bytes]:
    if piper.is_available():
        result = await piper.synthesize(text)
        if result:
            return result
    # Fall back to espeak
    return await espeak.synthesize(text)


def is_available() -> bool:
    return piper.is_available() or espeak.is_available()
