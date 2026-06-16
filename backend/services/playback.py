"""
Server-side broadcast engine for JellyFM.

Architecture per station (always-on, headless)
─────────────────────────────────────────────────────────────────────
  BroadcastWorker (asyncio.Task, started at server boot)
      │
      ├─ _tts_for_track()  →  Kokoro WAV bytes
      │       └─ _pipe_wav()  →  ffmpeg WAV decoder (WAV→PCM) ─┐
      │                                                          ├─► encoder stdin
      └─ _pipe_url()        →  ffmpeg URL decoder (URL→PCM)  ─┘
                                                                  │
                                              ffmpeg encoder (PCM → 256 kbps MP3)
                                                                  │
                                              _reader_task  reads MP3 chunks
                                                                  │
                                              broadcast to per-client asyncio.Queue
                                                                  │
                                           HTTP clients via subscribe()
─────────────────────────────────────────────────────────────────────

Error recovery (encoder crash guard)
─────────────────────────────────────
If the long-lived ffmpeg encoder exits unexpectedly (BrokenPipeError,
ConnectionResetError, or any other crash), _encoder_dead() detects it.
The loop does NOT advance the queue. Instead:
  1. It sleeps for a short back-off (1 s × consecutive_errors, max 30 s)
  2. _ensure_encoder() starts a fresh encoder + reader_task
  3. The same track replays from the start

This prevents the runaway song-cycling bug where a dead encoder was
previously mistaken for a completed/skipped track.

Skip isolation
──────────────
POST /skip sets p.skip_event only.  The loop detects it, clears it,
and advances exactly ONE track — no restarts, no crash path.
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

# Back-off constants for encoder crash recovery
_BACKOFF_BASE = 1.0   # seconds per consecutive failure
_BACKOFF_MAX  = 30.0  # hard ceiling


# ── ffmpeg command builders ───────────────────────────────────────────────────

def _enc_cmd() -> list[str]:
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", _PCM_FMT, "-ar", str(_RATE), "-ac", str(_CHANNELS), "-i", "pipe:0",
        "-c:a", "libmp3lame", "-b:a", _BITRATE, "-f", "mp3", "pipe:1",
    ]


def _dec_url_cmd(url: str) -> list[str]:
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", url,
        "-vn", "-f", _PCM_FMT, "-ar", str(_RATE), "-ac", str(_CHANNELS), "pipe:1",
    ]


def _dec_wav_cmd() -> list[str]:
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
        # Consecutive encoder-crash counter — drives back-off sleep duration
        self.consecutive_errors: int = 0

    def push(self, chunk: bytes) -> None:
        """Fan out an MP3 chunk to all connected subscribers."""
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
                q.put_nowait(None)   # sentinel — subscriber generator exits
            except Exception:
                pass
        self.subscribers.clear()


# ── Service ───────────────────────────────────────────────────────────────────

class PlaybackService:

    def __init__(self):
        self._players: dict[str, _Player] = {}

    # ── Public controls ───────────────────────────────────────────────────────

    async def start(self, station_id: str) -> None:
        """Start the always-on background broadcast for *station_id*.
        No-op if the worker is already running."""
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
        """Stop the broadcast and tear down the encoder.
        Called only on station deletion or server shutdown."""
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
        """Signal the broadcast worker to advance one track.
        Never restarts or crashes the encoder."""
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
        """Attach a new HTTP client to the live broadcast stream.
        Connecting and disconnecting never affects the background worker."""
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

    # ── Encoder lifecycle ─────────────────────────────────────────────────────

    def _encoder_dead(self, p: _Player) -> bool:
        return p.encoder_proc is None or p.encoder_proc.returncode is not None

    async def _ensure_encoder(self, p: _Player, sid: str) -> bool:
        """Start (or restart) the encoder if it has exited.
        Applies a back-off sleep proportional to consecutive_errors before
        restarting to prevent tight crash loops.
        Returns False only if ffmpeg is not installed (unrecoverable)."""
        if not self._encoder_dead(p):
            return True

        # Cancel stale reader task before spawning a new encoder
        if p.reader_task and not p.reader_task.done():
            p.reader_task.cancel()
            try:
                await p.reader_task
            except (asyncio.CancelledError, Exception):
                pass

        # Progressive back-off: 1 s, 2 s, 3 s … capped at 30 s
        if p.consecutive_errors > 0:
            backoff = min(_BACKOFF_BASE * p.consecutive_errors, _BACKOFF_MAX)
            logger.info(
                "[%s] Encoder restart back-off %.1f s (error streak: %d)",
                sid, backoff, p.consecutive_errors,
            )
            await asyncio.sleep(backoff)

        if p.stop_event.is_set():
            return False  # station was deleted during back-off sleep

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
        """Decode WAV announcement → PCM → encoder stdin.
        Returns False if interrupted by skip/stop or a pipe error."""
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
        """Decode audio from Jellyfin URL → PCM → encoder stdin.
        Returns True only when the track finishes naturally (EOF from decoder)."""
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

    # ── TTS ───────────────────────────────────────────────────────────────────

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

    async def _broadcast_loop(self, p: _Player) -> None:  # noqa: C901
        sid = p.station_id
        logger.info("Broadcast starting — station %s", sid)

        prefetched_tts: Optional[bytes] = None

        while not p.stop_event.is_set():

            # ── 1. Ensure encoder is alive ────────────────────────────────────
            # If the encoder crashed, _ensure_encoder sleeps (back-off) and
            # restarts it.  Queue position does NOT advance on crash.
            if not await self._ensure_encoder(p, sid):
                break  # ffmpeg missing — nothing we can do
            enc_in = p.encoder_proc.stdin

            # ── 2. Fetch current track ────────────────────────────────────────
            station = station_manager.get_station(sid)
            if station is None:
                logger.warning("[%s] Station disappeared", sid)
                break

            track = station.current_track
            if track is None:
                logger.info("[%s] Queue empty — refilling", sid)
                try:
                    station = await station_manager.refill_queue(sid)
                    track   = station.current_track
                except Exception:
                    logger.exception("[%s] Refill failed", sid)
                    await asyncio.sleep(5)
                    continue
                if track is None:
                    await asyncio.sleep(5)
                    continue

            # ── 3. TTS announcement ───────────────────────────────────────────
            tts = prefetched_tts or await self._tts_for_track(track, station.next_track)
            prefetched_tts = None

            if tts and not p.skip_event.is_set():
                await self._pipe_wav(tts, enc_in, p)

                # Encoder crash during TTS → back-off + restart, same track
                if self._encoder_dead(p):
                    p.consecutive_errors += 1
                    logger.warning(
                        "[%s] Encoder died during TTS (error #%d) — restarting",
                        sid, p.consecutive_errors,
                    )
                    p.skip_event.clear()
                    continue

            p.skip_event.clear()
            if p.stop_event.is_set():
                break

            # ── 4. Pre-fetch next TTS while this track plays ──────────────────
            next_track   = station.next_track
            prefetch_job = asyncio.create_task(
                self._tts_for_track(
                    next_track,
                    station.queue[station.current_index + 2]
                    if station.current_index + 2 < len(station.queue) else None,
                )
            ) if next_track else None

            # ── 5. Stream the music track ─────────────────────────────────────
            url = jellyfin.stream_url(track.id)
            logger.info("[%s] ▶ %s — %s", sid, track.artist, track.name)
            p.track_started_at = time.time()
            station.status     = StationStatus.playing

            completed = await self._pipe_url(url, enc_in, p)

            # Collect (or cancel) pre-fetched TTS
            if prefetch_job:
                if completed:
                    try:
                        prefetched_tts = await asyncio.wait_for(prefetch_job, timeout=3.0)
                    except asyncio.TimeoutError:
                        prefetch_job.cancel()
                else:
                    prefetch_job.cancel()

            # ── 6. Encoder crash guard ────────────────────────────────────────
            # A dead encoder after _pipe_url means a pipe/network failure,
            # NOT a skip.  Back off and restart WITHOUT advancing the queue.
            if self._encoder_dead(p):
                p.consecutive_errors += 1
                logger.warning(
                    "[%s] Encoder died during track (error #%d) — restarting same track",
                    sid, p.consecutive_errors,
                )
                p.skip_event.clear()
                continue

            # ── 7. Clean advance ──────────────────────────────────────────────
            # Only reached when the encoder is alive AND either:
            #   (a) track finished naturally (completed=True), or
            #   (b) user explicitly set skip_event (completed=False, encoder alive)
            p.consecutive_errors = 0   # successful track — reset error streak
            p.skip_event.clear()
            if p.stop_event.is_set():
                break
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
