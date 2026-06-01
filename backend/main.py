from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .routers import health, genres, stations, devices
from fastapi.staticfiles import StaticFiles
import os

app = FastAPI(title="JellyFM", version="0.1.0", description="Self-hosted radio powered by Jellyfin")

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
