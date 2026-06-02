from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel
from typing import Optional
import httpx

from ..models.station import Station
from ..models.jellyfin import Track
from ..services.station_manager import station_manager
from ..services.jellyfin import jellyfin
from ..services import tts_manager
from ..services.banter import get_banter

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
# Audio stream proxy
# Keeps the Jellyfin API key server-side; browser just hits this endpoint.
# Forwards Range headers so the browser can handle the stream correctly.
# ------------------------------------------------------------------

@router.get("/{station_id}/stream")
async def stream_audio(station_id: str, request: Request):
    station = station_manager.get_station(station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Station not found")
    track = station.current_track
    if not track:
        raise HTTPException(status_code=404, detail="No current track")

    upstream_url = jellyfin.stream_url(track.id)
    upstream_headers = {}
    if "range" in request.headers:
        upstream_headers["Range"] = request.headers["range"]

    async def generate():
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", upstream_url, headers=upstream_headers) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes(chunk_size=16384):
                    yield chunk

    return StreamingResponse(
        generate(),
        media_type="audio/mpeg",
        headers={"Accept-Ranges": "bytes"},
    )


# ------------------------------------------------------------------
# Playback queue controls (state only — browser drives actual audio)
# ------------------------------------------------------------------

@router.post("/{station_id}/skip", response_model=Optional[Track])
def skip(station_id: str):
    station = station_manager.get_station(station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Station not found")
    return station.advance()


@router.post("/{station_id}/previous", response_model=Optional[Track])
def previous(station_id: str):
    station = station_manager.get_station(station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Station not found")
    return station.rewind()


@router.post("/{station_id}/refill", response_model=Station)
async def refill_queue(station_id: str):
    try:
        return await station_manager.refill_queue(station_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Station not found")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# ------------------------------------------------------------------
# Server-side TTS announcement
# Used by browsers that block Web Speech API (e.g. GrapheneOS Vanadium).
# Returns WAV audio of the announcement, or 204 if espeak is unavailable.
# ------------------------------------------------------------------

@router.get("/{station_id}/announce")
async def announce(station_id: str):
    station = station_manager.get_station(station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Station not found")
    if not tts_manager.is_available():
        return Response(status_code=204)

    current = station.current_track
    if not current:
        return Response(status_code=204)

    banter = get_banter(current.genre)
    name   = current.tts_name   or current.name
    artist = current.tts_artist or current.artist
    text   = f"{banter}  Coming up: {name} by {artist}."

    nxt = station.next_track
    if nxt:
        n_name   = nxt.tts_name   or nxt.name
        n_artist = nxt.tts_artist or nxt.artist
        text += f"  And after that: {n_name} by {n_artist}."

    wav = await tts_manager.synthesize(text)
    if not wav:
        return Response(status_code=204)
    return Response(content=wav, media_type="audio/wav")


# ------------------------------------------------------------------
# Queue inspection
# ------------------------------------------------------------------

class NowPlaying(BaseModel):
    current: Optional[Track]
    next: Optional[Track]


@router.get("/{station_id}/now-playing", response_model=NowPlaying)
def now_playing(station_id: str):
    station = station_manager.get_station(station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Station not found")
    return NowPlaying(current=station.current_track, next=station.next_track)
