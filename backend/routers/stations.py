from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel
from typing import Optional

from ..models.station import Station
from ..models.jellyfin import Track
from ..services.station_manager import station_manager
from ..services.playback import playback_service

router = APIRouter(prefix="/stations", tags=["stations"])


# ------------------------------------------------------------------
# Request schemas
# ------------------------------------------------------------------

class CreateStationRequest(BaseModel):
    name: str
    genre: str
    shuffle: bool = True


# ------------------------------------------------------------------
# Station CRUD
# ------------------------------------------------------------------

@router.get("", response_model=list[Station])
def list_stations():
    return station_manager.list_stations()


@router.post("", response_model=Station, status_code=201)
async def create_station(body: CreateStationRequest):
    try:
        return await station_manager.create_station(body.name, body.genre, body.shuffle)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/{station_id}", response_model=Station)
def get_station(station_id: str):
    station = station_manager.get_station(station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Station not found")
    return station


@router.delete("/{station_id}", status_code=204)
async def delete_station(station_id: str):
    await playback_service.stop(station_id)
    if not station_manager.delete_station(station_id):
        raise HTTPException(status_code=404, detail="Station not found")


# ------------------------------------------------------------------
# Live broadcast stream
# Each client connecting here joins the same broadcast in progress.
# ------------------------------------------------------------------

@router.get("/{station_id}/stream")
async def stream_audio(station_id: str):
    station = station_manager.get_station(station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Station not found")

    await playback_service.start(station_id)

    return StreamingResponse(
        playback_service.subscribe(station_id),
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


# ------------------------------------------------------------------
# Playback controls
# ------------------------------------------------------------------

@router.post("/{station_id}/skip", status_code=204)
def skip(station_id: str):
    if not station_manager.get_station(station_id):
        raise HTTPException(status_code=404, detail="Station not found")
    playback_service.skip(station_id)


@router.post("/{station_id}/stop", status_code=204)
async def stop(station_id: str):
    if not station_manager.get_station(station_id):
        raise HTTPException(status_code=404, detail="Station not found")
    await playback_service.stop(station_id)


@router.post("/{station_id}/refill", response_model=Station)
async def refill_queue(station_id: str):
    try:
        return await station_manager.refill_queue(station_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Station not found")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# ------------------------------------------------------------------
# Now-playing (polled by UI every 2-3 s)
# ------------------------------------------------------------------

class NowPlaying(BaseModel):
    current: Optional[Track]
    next: Optional[Track]
    elapsed_seconds: Optional[float]
    is_live: bool


@router.get("/{station_id}/now-playing", response_model=NowPlaying)
def now_playing(station_id: str):
    station = station_manager.get_station(station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Station not found")
    return NowPlaying(
        current=station.current_track,
        next=station.next_track,
        elapsed_seconds=playback_service.get_elapsed(station_id),
        is_live=playback_service.is_running(station_id),
    )
