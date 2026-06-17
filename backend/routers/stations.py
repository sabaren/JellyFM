"""
Station API router — always-on broadcast design.

Every station's background broadcast worker runs 24/7, started at server
boot via the lifespan hook in main.py.  Client connections to /stream are
purely passive: they tap into the shared broadcast byte queue and receive
the live audio at whatever point the broadcast is currently at.

Connecting or disconnecting a client never starts, stops, pauses, or resets
the broadcast worker.  The worker only stops when the station is deleted.

Proxy sessions (/sessions endpoints)
──────────────────────────────────────
A proxy session wraps a subscriber queue with a stable ID so the browser
never needs to change audio.src.  The session is created once (POST /sessions),
the audio element src is set to GET /sessions/{id} and never changed again.
Switching channels is a single PUT /sessions/{id}/station call — the server
atomically re-subscribes the session to the new broadcast mid-stream.
"""
import logging
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel
from typing import Optional

from ..models.station import Station
from ..models.jellyfin import Track
from ..services.station_manager import station_manager
from ..services.playback import playback_service
from ..services import tts_manager
from ..services.banter import get_banter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stations", tags=["stations"])


# ── Station CRUD ──────────────────────────────────────────────────────────────

class CreateStationRequest(BaseModel):
    name: str
    genre: str
    shuffle: bool = True


@router.get("", response_model=list[Station])
def list_stations():
    return station_manager.list_stations()


@router.post("", response_model=Station, status_code=201)
async def create_station(body: CreateStationRequest):
    try:
        station = await station_manager.create_station(
            body.name, body.genre, body.shuffle
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    # Auto-start the always-on broadcast immediately after creation
    await playback_service.start(station.id)
    return station


@router.get("/{station_id}", response_model=Station)
def get_station(station_id: str):
    station = station_manager.get_station(station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Station not found")
    return station


@router.delete("/{station_id}", status_code=204)
async def delete_station(station_id: str):
    """Stop the broadcast worker, then remove the station record."""
    await playback_service.stop(station_id)
    if not station_manager.delete_station(station_id):
        raise HTTPException(status_code=404, detail="Station not found")


# ── Live broadcast stream ─────────────────────────────────────────────────────

@router.get("/{station_id}/stream")
async def stream_audio(station_id: str):
    """
    Passive subscriber tap into the station's live broadcast.

    The background worker is already running and continuously encoding audio.
    This endpoint only subscribes the HTTP connection to the worker's shared
    broadcast queue.  The subscriber queue is pre-seeded with the last ~4
    seconds of encoded audio (burst-on-connect) so the browser syncs to the
    live position immediately without buffering silently first.

    Closing this connection (browser tab closed, Stop button) only removes
    the subscriber from the fan-out list.  The broadcast worker is unaffected
    and continues running for other connected clients and future listeners.
    """
    station = station_manager.get_station(station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Station not found")

    # Safety net: start the worker if it somehow isn't running yet.
    # Under normal operation (lifespan hook) this branch is never taken.
    if not playback_service.is_running(station_id):
        await playback_service.start(station_id)

    return StreamingResponse(
        playback_service.subscribe(station_id),
        media_type="audio/mpeg",
        headers={
            "Cache-Control":         "no-cache, no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Accel-Buffering":     "no",
        },
    )


# ── Proxy sessions (seamless channel switching) ───────────────────────────────

class CreateSessionRequest(BaseModel):
    station_id: str


class SwitchSessionRequest(BaseModel):
    station_id: str


@router.post("/sessions", status_code=201)
async def create_session(body: CreateSessionRequest):
    """
    Create a persistent proxy session pre-subscribed to a station.

    The browser sets audio.src = /stations/sessions/{session_id} exactly once
    and never changes it.  Channel switching is done via PUT
    /stations/sessions/{session_id}/station so the HTTP connection stays open.
    """
    station = station_manager.get_station(body.station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Station not found")
    if not playback_service.is_running(body.station_id):
        await playback_service.start(body.station_id)
    session_id = playback_service.create_session(body.station_id)
    if not session_id:
        raise HTTPException(status_code=503, detail="Broadcast not running")
    return {"session_id": session_id}


@router.get("/sessions/{session_id}")
async def stream_session(session_id: str):
    """
    Stream audio for an existing proxy session.

    The response stays open indefinitely.  The server switches which station's
    broadcast feeds this stream when the client calls PUT .../station.
    """
    if session_id not in playback_service._sessions:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    return StreamingResponse(
        playback_service.stream_session(session_id),
        media_type="audio/mpeg",
        headers={
            "Cache-Control":         "no-cache, no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Accel-Buffering":     "no",
        },
    )


@router.put("/sessions/{session_id}/station", status_code=204)
async def switch_session_station(session_id: str, body: SwitchSessionRequest):
    """
    Switch the station a proxy session is subscribed to without closing the
    audio connection.  The server drains stale audio, seeds the new station's
    burst buffer, and re-attaches the subscriber queue — all server-side.
    """
    station = station_manager.get_station(body.station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Station not found")
    if not playback_service.is_running(body.station_id):
        await playback_service.start(body.station_id)
    ok = playback_service.switch_session(session_id, body.station_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found or expired")


# ── Playback controls ─────────────────────────────────────────────────────────

@router.post("/{station_id}/skip", status_code=204)
def skip(station_id: str):
    """
    Advance the broadcast exactly one track by setting the worker's skip_event.
    This never restarts the encoder or causes a crash path — it is a clean
    signal that the broadcast loop handles at the next safe checkpoint.
    """
    if not station_manager.get_station(station_id):
        raise HTTPException(status_code=404, detail="Station not found")
    playback_service.skip(station_id)


@router.post("/{station_id}/refill", response_model=Station)
async def refill_queue(station_id: str):
    """Re-fetch tracks for this station's genre from Jellyfin."""
    try:
        return await station_manager.refill_queue(station_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Station not found")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# ── TTS announcement preview ──────────────────────────────────────────────────

@router.get("/{station_id}/announce")
async def announce(station_id: str):
    """
    Synthesise the current track's announcement and return it as WAV audio.
    Used by the voice preview button in the UI.  Returns 204 if TTS is
    unavailable or no track is currently queued.
    """
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


# ── Now-playing metadata ──────────────────────────────────────────────────────

class NowPlaying(BaseModel):
    current:         Optional[Track]
    next:            Optional[Track]
    elapsed_seconds: Optional[float]
    is_live:         bool


@router.get("/{station_id}/now-playing", response_model=NowPlaying)
def now_playing(station_id: str):
    """
    Returns current track metadata and elapsed time sourced directly from
    the broadcast worker's runtime state variables.

    elapsed_seconds is computed from the worker's p.track_started_at wall-clock
    timestamp.  Because station.advance() is only called after the drain wait
    (when the encoder buffer has fully flushed), elapsed_seconds and
    station.current_track are always in sync with what listeners actually hear.
    """
    station = station_manager.get_station(station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Station not found")
    return NowPlaying(
        current=station.current_track,
        next=station.next_track,
        elapsed_seconds=playback_service.get_elapsed(station_id),
        is_live=playback_service.is_running(station_id),
    )
