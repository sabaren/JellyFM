"""
Piper neural TTS service.

Piper is invoked as a subprocess: text is written to stdin, WAV is written
to a temp file and read back. This is the most portable approach across
Piper versions and avoids /dev/stdout issues on some systems.

Expected layout (matches the README install instructions):
    ~/piper/piper          ← binary
    ~/piper/voices/*.onnx  ← voice models
    ~/piper/voices/*.onnx.json  ← model configs (must sit next to .onnx)
"""
import asyncio
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Locate binary — prefer ~/piper/piper, fall back to PATH
_HOME_BIN = Path.home() / "piper" / "piper"
_PIPER = str(_HOME_BIN) if _HOME_BIN.exists() else shutil.which("piper")

_VOICES_DIR = Path.home() / "piper" / "voices"

# Active model path — set via set_model()
_active_model: Optional[Path] = None


# ------------------------------------------------------------------
# Model discovery
# ------------------------------------------------------------------

def _label(path: Path) -> str:
    """Turn 'en_US-amy-medium.onnx' into 'Amy (US) · Medium'."""
    stem = path.name.replace(".onnx", "")
    parts = stem.split("-")
    # parts: ['en_US', 'amy', 'medium']  (or more)
    lang  = parts[0].replace("_", "-") if parts else ""
    name  = parts[1].title() if len(parts) > 1 else stem
    qual  = parts[2].title() if len(parts) > 2 else ""
    return f"{name} ({lang}){' · ' + qual if qual else ''}"


def list_models() -> list[dict]:
    """Return all .onnx voice models found in the voices directory."""
    if not _VOICES_DIR.is_dir():
        return []
    models = []
    for f in sorted(_VOICES_DIR.glob("*.onnx")):
        cfg = f.with_suffix(".onnx.json")
        models.append({
            "id": f.name,
            "label": _label(f),
            "path": str(f),
            "has_config": cfg.exists(),
        })
    return models


def is_available() -> bool:
    return bool(_PIPER) and bool(list_models())


def set_model(model_filename: str) -> bool:
    """Select a model by filename (e.g. 'en_US-amy-medium.onnx')."""
    global _active_model
    candidate = _VOICES_DIR / model_filename
    if candidate.exists():
        _active_model = candidate
        return True
    return False


def _get_model() -> Optional[Path]:
    if _active_model and _active_model.exists():
        return _active_model
    # Auto-pick first available model
    models = list_models()
    if models:
        return Path(models[0]["path"])
    return None


# ------------------------------------------------------------------
# Synthesis
# ------------------------------------------------------------------

async def synthesize(text: str) -> Optional[bytes]:
    """Synthesize text to WAV bytes. Returns None on any failure."""
    if not _PIPER:
        return None
    model = _get_model()
    if not model:
        return None

    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmppath = tmp.name

        proc = await asyncio.create_subprocess_exec(
            _PIPER,
            "--model", str(model),
            "--output_file", tmppath,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(
            proc.communicate(input=text.encode()),
            timeout=30.0,
        )
        if proc.returncode == 0:
            with open(tmppath, "rb") as f:
                return f.read()
    except asyncio.TimeoutError:
        logger.warning("Piper timed out for text: %r", text[:60])
    except Exception:
        logger.exception("Piper synthesis failed")
    finally:
        try:
            os.unlink(tmppath)
        except Exception:
            pass
    return None
