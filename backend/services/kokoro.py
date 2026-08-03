"""
Kokoro ONNX TTS service — studio-quality, fully offline voice synthesis.

Expected model layout (see README for download instructions):
    ~/kokoro/kokoro-v1.0.onnx      ← ONNX model weights
    ~/kokoro/voices-v1.0.bin        ← voice embeddings (numpy binary)

Dependencies: kokoro-onnx, soundfile, onnxruntime, numpy
"""
import asyncio
import io
import logging
import threading
from pathlib import Path
from typing import Optional, Union

import numpy as np

logger = logging.getLogger(__name__)

_MODEL_DIR   = Path.home() / "kokoro"
_MODEL_FILE  = _MODEL_DIR / "kokoro-v1.0.onnx"
_VOICES_FILE = _MODEL_DIR / "voices-v1.0.bin"

# ── Base voice catalogue ──────────────────────────────────────────────────────

_BASE_VOICES: dict[str, str] = {
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

# ── Blended persona definitions ───────────────────────────────────────────────
# Each blend is a weighted average of base voice style vectors.
# Weights are automatically L1-normalised so they sum to 1.0.

_BLENDS: dict[str, dict] = {
    "blend_smooth_host": {
        "label":   "Smooth Radio Host",
        "sources": [("am_adam", 0.60), ("am_michael", 0.40)],
    },
    "blend_warm_host": {
        "label":   "Warm Evening Host",
        "sources": [("af_bella", 0.70), ("af_sarah", 0.30)],
    },
    "blend_bbc_host": {
        "label":   "BBC Style Host",
        "sources": [("bm_george", 0.55), ("bf_emma", 0.45)],
    },
    "blend_chill_host": {
        "label":   "Chill Late-Night Host",
        "sources": [("af_sky", 0.50), ("am_adam", 0.30), ("af_nicole", 0.20)],
    },
}

# Public voice catalogue (base + blends)
VOICES: dict[str, str] = {
    **_BASE_VOICES,
    **{bid: d["label"] for bid, d in _BLENDS.items()},
}

# ── Module-level state ────────────────────────────────────────────────────────

_active_voice: str = "af_bella"
_instance     = None
_init_lock    = threading.Lock()

# Sequential synthesis lock — prevents concurrent CPU-bound inference jobs
# from context-thrashing the Pi's cores. Must be threading.Lock (not
# asyncio.Lock) because run_in_executor spawns real threads that bypass
# asyncio.Lock protections entirely.
_synthesis_lock = threading.Lock()


# ── Initialisation ────────────────────────────────────────────────────────────

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
            import onnxruntime as ort  # type: ignore
            from kokoro_onnx import Kokoro  # type: ignore

            # Tune ORT for Pi 5: 4 intra-op threads (one per core), 1 inter-op
            # thread to avoid scheduling overhead between graph partitions.
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 4
            opts.inter_op_num_threads = 1
            opts.execution_mode      = ort.ExecutionMode.ORT_SEQUENTIAL

            _instance = Kokoro(str(_MODEL_FILE), str(_VOICES_FILE), sess_options=opts)
            logger.info("Kokoro TTS ready — %s (4 ORT threads)", _MODEL_FILE.name)
        except TypeError:
            # Older kokoro-onnx versions don't accept sess_options — fall back
            try:
                from kokoro_onnx import Kokoro  # type: ignore
                _instance = Kokoro(str(_MODEL_FILE), str(_VOICES_FILE))
                logger.info("Kokoro TTS ready — %s (default ORT threads)", _MODEL_FILE.name)
            except Exception:
                logger.exception("Kokoro TTS initialisation failed")
        except Exception:
            logger.exception("Kokoro TTS initialisation failed")
    return _instance


def is_available() -> bool:
    return _get_instance() is not None


# ── Voice management ──────────────────────────────────────────────────────────

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


def _resolve_voice_vector(instance, voice_id: str) -> Union[str, np.ndarray]:
    """
    Return a voice style vector (or plain string) for `voice_id`.
    Blended IDs are computed as a weighted average of their source vectors.
    Falls back to the plain string if voice data is inaccessible.
    """
    if voice_id not in _BLENDS:
        return voice_id  # base voice — kokoro resolves string internally

    blend_def = _BLENDS[voice_id]
    voices_dict = getattr(instance, "voices", None)
    if voices_dict is None:
        logger.warning("Kokoro instance has no .voices — blend not supported, using af_bella")
        return "af_bella"

    vecs:    list[np.ndarray] = []
    weights: list[float]      = []
    for src_id, w in blend_def["sources"]:
        if src_id in voices_dict:
            vecs.append(voices_dict[src_id])
            weights.append(w)
        else:
            logger.debug("Blend source %r not found in voices", src_id)

    if not vecs:
        return "af_bella"

    total = sum(weights)
    blended: np.ndarray = sum((w / total) * v for w, v in zip(weights, vecs))
    return blended


# ── Synthesis ─────────────────────────────────────────────────────────────────

async def synthesize(text: str) -> Optional[bytes]:
    """Synthesize *text* to 16-bit PCM WAV bytes.

    Guarded by _synthesis_lock so at most one inference job runs at a time,
    preventing CPU context-thrashing on the Pi 5 from pre-fetch concurrency.
    """
    instance = _get_instance()
    if not instance:
        return None

    with _synthesis_lock:
        try:
            import soundfile as sf  # type: ignore

            voice = _active_voice
            loop  = asyncio.get_running_loop()

            def _run() -> bytes:
                voice_arg = _resolve_voice_vector(instance, voice)
                samples, sample_rate = instance.create(
                    text, voice=voice_arg, speed=1.05, lang="en-us"
                )
                buf = io.BytesIO()
                sf.write(buf, samples, sample_rate, format="WAV", subtype="PCM_16")
                return buf.getvalue()

            return await asyncio.wait_for(
                loop.run_in_executor(None, _run),
                timeout=60.0,
            )
        except asyncio.TimeoutError:
            logger.warning("Kokoro TTS timed out for: %r", text[:60])
        except Exception:
            logger.exception("Kokoro TTS synthesis failed")
    return None
