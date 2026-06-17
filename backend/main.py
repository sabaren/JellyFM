"""
JellyFM application entry point.

Lifespan hook: every station persisted to stations.json is started as a
headless background broadcast worker at server boot.  Stations run 24/7
regardless of whether any browser client is connected.  New stations
created via the API are also auto-started immediately.  Workers are only
stopped when their station is explicitly deleted.

TTS warm-up: Kokoro ONNX compilation is triggered as a background task
immediately at boot so the first track announcement is instant rather than
blocking for 10-20 seconds on cold JIT compilation.  Because kokoro.py
holds _synthesis_lock during synthesis, any broadcast-loop TTS call that
races with warm-up will simply wait on the lock rather than experiencing
the cold-start penalty.
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import os

from .routers import health, genres, stations, devices
from .services.station_manager import station_manager
from .services.playback import playback_service
from .services import tts_manager

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ───────────────────────────────────────────────────────────────

    # Fire TTS warm-up as a background task.  This triggers ONNX JIT
    # compilation before any real announcement is requested.  Station
    # broadcast loops start immediately in parallel — if a loop reaches its
    # first TTS call before warm-up finishes, it waits on _synthesis_lock
    # rather than cold-starting the model itself.
    asyncio.create_task(tts_manager.warmup(), name="tts-warmup")

    persisted = station_manager.list_stations()
    if persisted:
        logger.info("Auto-starting %d persisted station broadcast(s)…", len(persisted))
        for station in persisted:
            await playback_service.start(station.id)
            logger.info("  ▶ %s (%s)", station.name, station.id)
    else:
        logger.info("No persisted stations — broadcasts will start when stations are created.")

    yield

    # ── Shutdown ──────────────────────────────────────────────────────────────
    running = list(station_manager.list_stations())
    if running:
        logger.info("Shutting down %d station broadcast(s)…", len(running))
        for station in running:
            await playback_service.stop(station.id)


app = FastAPI(
    title="JellyFM",
    version="0.2.0",
    description="Self-hosted radio powered by Jellyfin",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(genres.router)
app.include_router(stations.router)
app.include_router(devices.router)

_static = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(_static):
    app.mount("/", StaticFiles(directory=_static, html=True), name="static")
