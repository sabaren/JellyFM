"""
Server-side broadcast engine for JellyFM.

Architecture per station
─────────────────────────────────────────────────────────────────────
  BroadcastWorker (asyncio.Task)
      │
      ├─ _tts_for_track()  →  Kokoro WAV bytes
      │       │
      │       └─ _pipe_wav()  →  ffmpeg decoder (WAV→PCM)  ─┐
      │                                                       ├─► encoder stdin
      └─ _pipe_url()        →  ffmpeg decoder (URL→PCM)   ─┘
                                                               │
                                             ffmpeg encoder (PCM→256kbps MP3)
                                                               │
                                             _reader_task reads stdout
                                                               │
                                             broadcast to subscriber Queue list
                                                               │
                                          HTTP clients via subscribe()
─────────────────────────────────────────────────────────────────────

Skip: sets skip_event → decoder is killed → worker advances queue by exactly
one track.

Encoder crash guard: if the long-lived ffmpeg encoder exits unexpectedly
(BrokenPipeError etc.), _encoder_dead() detects this and _ensure_encoder()
restarts a fresh encoder process WITHOUT advancing the queue.  This prevents
the runaway song-cycling bug where encoder crashes were previously mistaken
for completed tracks.
"""

import asyncio
import logging
import time
from typing import Optional, AsyncGenerator

from ..models.station import StationStatus
from ..services.station_manager import station_manager
from ..services.jellyfin import jellyfin
from ..services import tts_manager
from ..services.banter import get_banter

logger = logging.getLogger(__name__)

_RATE     = 44100
_CHANNELS = 2
_PCM_FMT  = "s16le"
_BITRATE  = "256k"
_CHUNK    = 8192
_Q_MAX    = 256


# ── ffmpeg command builders ───────────────────────────────────────────────────

def _enc_cmd() -> list[str]:
    """Long-lived MP3 encoder: raw PCM stdin → MP3 stdout."""
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", _PCM_FMT, "-ar", str(_RATE), "-ac", str(_CHANNELS), "-i", "pipe:0",
        "-c:a", "libmp3lame", "-b:a", _BITRATE, "-f", "mp3", "pipe:1",
    ]


def _dec_url_cmd(url: str) -> list[str]:
    """Per-track decoder: Jellyfin URL → raw PCM stdout."""
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", url,
        "-vn", "-f", _PCM_FMT, "-ar", str(_RATE), "-ac", str(_CHANNELS), "pipe:1",
    ]


def _dec_wav_cmd() -> list[str]:
    """Per-announcement decoder: WAV bytes stdin → raw PCM stdout."""
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", "pipe:0",
        "-vn", "-f", _PCM_FMT, "-ar", str(_RATE), "-ac", str(_CHANNELS), "pipe:1",
    ]


# ── Per-station state ─────────────────────────────────────────────────────────

class _Player:
    def __init__(self, station_id: str):
        self.station_id    = station_id
        self.skip_event    = asyncio.Event()
        self.stop_event    = asyncio.Event()
        self.subscribers:  list[asyncio.Queue] = []
        self.encoder_proc: Optional[asyncio.subprocess.Process] = None
        self.task:         Optional[asyncio.Task] = None
        self.reader_task:  Optional[asyncio.Task] = None
        self.track_started_at: Optional[float] = None

    def push(self, chunk: bytes) -> None:
        """Broadcast a chunk to all subscribers; silently drop laggy ones."""
        dead = []
        for q in self.subscribers:
            try:
                q.put_nowait(chunk)
            except asyncio.QueueFull:
                dead.append(q)
        for d in dead:
            try:
                self.subscribers.remove(d)
            except ValueError:
                pass

    def shutdown_subscribers(self) -> None:
        for q in self.subscribers:
            try:
                q.put_nowait(None)   # sentinel → subscriber generator exits
            except Exception:
                pass
        self.subscribers.clear()


# ── Service ───────────────────────────────────────────────────────────────────

class PlaybackService:

    def __init__(self):
        self._players: dict[str, _Player] = {}

    # ── Public controls ───────────────────────────────────────────────────────

    async def start(self, station_id: str) -> None:
        if station_id in self._players:
            return
        p = _Player(station_id)
        self._players[station_id] = p
        p.task = asyncio.create_task(
            self._broadcast_loop(p),
            name=f"broadcast-{station_id}",
        )
        p.task.add_done_callback(lambda t: self._on_done(station_id, t))

    async def stop(self, station_id: str) -> None:
        p = self._players.pop(station_id, None)
        if not p:
            return
        p.stop_event.set()
        p.skip_event.set()
        if p.encoder_proc:
            try:
                p.encoder_proc.kill()
            except Exception:
                pass
        if p.task:
            try:
                await asyncio.wait_for(p.task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                p.task.cancel()
        p.shutdown_subscribers()

    def skip(self, station_id: str) -> None:
        p = self._players.get(station_id)
        if p:
            p.skip_event.set()

    def is_running(self, station_id: str) -> bool:
        return station_id in self._players

    def get_elapsed(self, station_id: str) -> Optional[float]:
        p = self._players.get(station_id)
        if p and p.track_started_at:
            return time.time() - p.track_started_at
        return None

    async def subscribe(self, station_id: str) -> AsyncGenerator[bytes, None]:
        """Yield live MP3 chunks. Attaches to the running broadcast; disconnects cleanly."""
        p = self._players.get(station_id)
        if not p:
            return
        q: asyncio.Queue = asyncio.Queue(maxsize=_Q_MAX)
        p.subscribers.append(q)
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(q.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    if not self.is_running(station_id):
                        break
                    continue
                if chunk is None:
                    break
                yield chunk
        except asyncio.CancelledError:
            pass
        finally:
            try:
                p.subscribers.remove(q)
            except ValueError:
                pass

    # ── Encoder lifecycle helpers ─────────────────────────────────────────────

    def _encoder_dead(self, p: _Player) -> bool:
        """True if the encoder process has exited or never been started."""
        return p.encoder_proc is None or p.encoder_proc.returncode is not None

    async def _ensure_encoder(self, p: _Player, sid: str) -> bool:
        """
        (Re)start the encoder if it has died.  Cancels the stale reader task
        and spawns a fresh one.  Returns False only when ffmpeg is missing
        (unrecoverable).
        """
        if not self._encoder_dead(p):
            return True

        # Cancel stale reader before starting a new encoder
        if p.reader_task and not p.reader_task.done():
            p.reader_task.cancel()
            try:
                await p.reader_task
            except (asyncio.CancelledError, Exception):
                pass

        logger.info("[%s] (Re)starting encoder", sid)
        try:
            p.encoder_proc = await asyncio.create_subprocess_exec(
                *_enc_cmd(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            logger.error("ffmpeg not found — install with: sudo apt install ffmpeg")
            return False

        p.reader_task = asyncio.create_task(
            self._reader_loop(p), name=f"reader-{sid}"
        )
        return True

    # ── Reader task ───────────────────────────────────────────────────────────

    async def _reader_loop(self, p: _Player) -> None:
        """Read encoded MP3 from the encoder and broadcast to all subscribers."""
        while not p.stop_event.is_set():
            try:
                chunk = await asyncio.wait_for(
                    p.encoder_proc.stdout.read(_CHUNK), timeout=10.0
                )
            except asyncio.TimeoutError:
                continue
            except Exception:
                break
            if not chunk:
                break
            p.push(chunk)

    # ── PCM pipe helpers ──────────────────────────────────────────────────────

    async def _pipe_wav(self, wav: bytes, enc_in: asyncio.StreamWriter, p: _Player) -> bool:
        """Decode WAV announcement to PCM and feed encoder. Returns False if interrupted."""
        proc = await asyncio.create_subprocess_exec(
            *_dec_wav_cmd(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            pcm, _ = await asyncio.wait_for(proc.communicate(input=wav), timeout=30.0)
        except asyncio.TimeoutError:
            proc.kill()
            return False

        for off in range(0, len(pcm), _CHUNK):
            if p.skip_event.is_set() or p.stop_event.is_set():
                return False
            try:
                enc_in.write(pcm[off: off + _CHUNK])
                await enc_in.drain()
            except (BrokenPipeError, ConnectionResetError):
                return False
        return True

    async def _pipe_url(self, url: str, enc_in: asyncio.StreamWriter, p: _Player) -> bool:
        """Decode audio from Jellyfin URL to PCM and feed encoder.
        Returns True only when the track finished naturally."""
        proc = await asyncio.create_subprocess_exec(
            *_dec_url_cmd(url),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        completed = False
        try:
            while not p.skip_event.is_set() and not p.stop_event.is_set():
                try:
                    chunk = await asyncio.wait_for(
                        proc.stdout.read(_CHUNK), timeout=15.0
                    )
                except asyncio.TimeoutError:
                    logger.warning("[%s] Decoder read timeout", p.station_id)
                    break
                if not chunk:
                    completed = True
                    break
                try:
                    enc_in.write(chunk)
                    await enc_in.drain()
                except (BrokenPipeError, ConnectionResetError):
                    break
        finally:
            try:
                proc.kill()
            except Exception:
                pass
            await proc.wait()
        return completed

    # ── TTS generation ────────────────────────────────────────────────────────

    async def _tts_for_track(self, track, next_track=None) -> Optional[bytes]:
        banter = get_banter(track.genre)
        name   = track.tts_name   or track.name
        artist = track.tts_artist or track.artist
        text   = f"{banter}  Coming up: {name} by {artist}."
        if next_track:
            nn = next_track.tts_name   or next_track.name
            na = next_track.tts_artist or next_track.artist
            text += f"  And after that: {nn} by {na}."
        return await tts_manager.synthesize(text)

    # ── Main broadcast loop ───────────────────────────────────────────────────

    async def _broadcast_loop(self, p: _Player) -> None:
        sid = p.station_id
        logger.info("Broadcast starting — station %s", sid)

        prefetched_tts: Optional[bytes] = None

        while not p.stop_event.is_set():

            # ── Ensure encoder is running (restarts without advancing queue) ──
            if not await self._ensure_encoder(p, sid):
                break  # ffmpeg missing — unrecoverable
            enc_in = p.encoder_proc.stdin

            # ── Get station & current track ───────────────────────────────────
            station = station_manager.get_station(sid)
            if station is None:
                logger.warning("Station %s disappeared", sid)
                break

            track = station.current_track
            if track is None:
                logger.info("[%s] Queue empty — refilling", sid)
                try:
                    station = await station_manager.refill_queue(sid)
                    track   = station.current_track
                except Exception:
                    logger.exception("Refill failed for %s", sid)
                    await asyncio.sleep(5)
                    continue
                if track is None:
                    await asyncio.sleep(5)
                    continue

            # ── Pipe TTS announcement ─────────────────────────────────────────
            tts = prefetched_tts or await self._tts_for_track(track, station.next_track)
            prefetched_tts = None

            if tts and not p.skip_event.is_set():
                await self._pipe_wav(tts, enc_in, p)

                # Encoder died during TTS → restart next iteration, same track
                if self._encoder_dead(p):
                    logger.warning("[%s] Encoder died during TTS — restarting", sid)
                    p.skip_event.clear()
                    continue

            p.skip_event.clear()
            if p.stop_event.is_set():
                break

            # ── Pre-fetch TTS for next track concurrently ─────────────────────
            next_track   = station.next_track
            prefetch_job = asyncio.create_task(
                self._tts_for_track(
                    next_track,
                    station.queue[station.current_index + 2]
                    if station.current_index + 2 < len(station.queue) else None,
                )
            ) if next_track else None

            # ── Pipe the music track ──────────────────────────────────────────
            url = jellyfin.stream_url(track.id)
            logger.info("[%s] ▶ %s — %s", sid, track.artist, track.name)
            p.track_started_at = time.time()
            station.status     = StationStatus.playing

            completed = await self._pipe_url(url, enc_in, p)

            # Collect pre-fetched TTS result
            if prefetch_job:
                if completed:
                    try:
                        prefetched_tts = await asyncio.wait_for(prefetch_job, timeout=3.0)
                    except asyncio.TimeoutError:
                        prefetch_job.cancel()
                else:
                    prefetch_job.cancel()

            # ── Encoder crash guard ───────────────────────────────────────────
            # If the encoder died during the track (BrokenPipeError etc.),
            # restart it WITHOUT advancing the queue.  This is the key fix for
            # the runaway song-cycling bug: previously a dead encoder would
            # return completed=False, which looked identical to a skip, causing
            # an infinite advance loop.
            if self._encoder_dead(p):
                logger.warning("[%s] Encoder died during track — restarting", sid)
                p.skip_event.clear()
                continue

            p.skip_event.clear()
            if p.stop_event.is_set():
                break

            # Track finished naturally or user explicitly skipped → advance once
            station.advance()

        # ── Teardown ──────────────────────────────────────────────────────────
        logger.info("Broadcast ending — station %s", sid)
        st = station_manager.get_station(sid)
        if st:
            st.status = StationStatus.idle
        if p.encoder_proc:
            try:
                p.encoder_proc.stdin.close()
            except Exception:
                pass
            try:
                p.encoder_proc.kill()
                await p.encoder_proc.wait()
            except Exception:
                pass
        if p.reader_task:
            p.reader_task.cancel()
        p.shutdown_subscribers()

    def _on_done(self, station_id: str, task: asyncio.Task) -> None:
        self._players.pop(station_id, None)
        if not task.cancelled() and task.exception():
            logger.error(
                "Broadcast crashed — %s", station_id, exc_info=task.exception()
            )


playback_service = PlaybackService()
