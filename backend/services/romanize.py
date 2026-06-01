"""
Romanize CJK text to latin script so TTS engines can pronounce
track/artist names instead of saying "Japanese character".

Uses pykakasi which handles kanji → romaji at the morpheme level.
Non-CJK text passes through unchanged.
"""
import re

_kakasi = None


def _get_kakasi():
    global _kakasi
    if _kakasi is None:
        import pykakasi
        _kakasi = pykakasi.kakasi()
    return _kakasi


def _has_cjk(text: str) -> bool:
    for ch in text:
        cp = ord(ch)
        if (
            0x3000 <= cp <= 0x9FFF    # CJK unified, hiragana, katakana
            or 0xF900 <= cp <= 0xFAFF  # CJK compatibility
            or 0x20000 <= cp <= 0x2A6DF
        ):
            return True
    return False


def romanize(text: str) -> str:
    """Return a latin-script version of *text*. Non-CJK text is returned as-is."""
    if not text or not _has_cjk(text):
        return text
    try:
        kks = _get_kakasi()
        parts = []
        for item in kks.convert(text):
            # hepburn is the most natural romanization for an English TTS voice
            chunk = item.get("hepburn") or item.get("orig", "")
            if chunk:
                parts.append(chunk)
        romanized = re.sub(r" {2,}", " ", " ".join(parts)).strip()
        return romanized if romanized else text
    except Exception:
        return text
