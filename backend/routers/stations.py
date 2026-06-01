from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from ..models.station import Station
from ..models.jellyfin import Track
from ..services.station_manager import station_manager
from ..services.jellyfin import jellyfin

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
def delete_station(station_id: str):
    if not station_manager.delete_station(station_id):
        raise HTTPException(status_code=404, detail="Station not found")


# ------------------------------------------------------------------
# Playback controls
# ------------------------------------------------------------------

@router.post("/{station_id}/play", response_model=Optional[Track])
def play(station_id: str):
    try:
        return station_manager.play(station_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Station not found")


@router.post("/{station_id}/pause", status_code=204)
def pause(station_id: str):
    try:
        station_manager.pause(station_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Station not found")


@router.post("/{station_id}/skip", response_model=Optional[Track])
def skip(station_id: str):
    try:
        return station_manager.skip(station_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Station not found")


@router.post("/{station_id}/previous", response_model=Optional[Track])
def previous(station_id: str):
    try:
        return station_manager.previous(station_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Station not found")


@router.post("/{station_id}/refill", response_model=Station)
async def refill_queue(station_id: str):
    try:
        return await station_manager.refill_queue(station_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Station not found")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# ------------------------------------------------------------------
# Queue inspection
# ------------------------------------------------------------------

class NowPlaying(BaseModel):
    current: Optional[Track]
    next: Optional[Track]
    stream_url: Optional[str] = None


@router.get("/{station_id}/now-playing", response_model=NowPlaying)
def now_playing(station_id: str):
    station = station_manager.get_station(station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Station not found")
    current = station.current_track
    return NowPlaying(
        current=current,
        next=station.next_track,
        stream_url=jellyfin.stream_url(current.id) if current else None,
    )
