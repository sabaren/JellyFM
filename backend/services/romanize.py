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
    """Check if text contains actual CJK characters (CJK Unified Ideographs, Hiragana, Katakana, Hangul).
    
    Narrowed ranges to exclude decorative symbols (0x2600-27BF), dingbats, 
    and compatibility forms that aren't actual CJK script.
    """
    for ch in text:
        cp = ord(ch)
        if (
            0x4E00 <= cp <= 0x9FFF     # CJK Unified Ideographs
            or 0x3040 <= cp <= 0x309F   # Hiragana
            or 0x30A0 <= cp <= 0x30FF   # Katakana
            or 0x31F0 <= cp <= 0x31FF   # Katakana Phonetic Extensions
            or 0xAC00 <= cp <= 0xD7AF   # Hangul Syllables
            or 0xF900 <= cp <= 0xFAFF   # CJK Compatibility Ideographs
            or 0x20000 <= cp <= 0x2A6DF  # CJK Extension A
            or 0x2A700 <= cp <= 0x2B73F  # CJK Extension B
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
