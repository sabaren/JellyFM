"""
Server-side broadcast engine for JellyFM — always-on, shared live stream.

Architecture per station
─────────────────────────────────────────────────────────────────────
  BroadcastWorker (asyncio.Task, started at server boot)
      │
      ├─ _tts_for_track()  →  Kokoro WAV bytes
      │       └─ _pipe_wav()  →  ffmpeg WAV decoder (WAV→PCM)  ─┐
      │                                                            ├─► encoder stdin
      └─ _pipe_url()        →  ffmpeg URL decoder (URL→PCM)    ─┘
                                                                    │
                                           ffmpeg encoder (PCM → 256 kbps CBR MP3)
                                                                    │
                                           _reader_task  reads MP3 stdout
                                                                    │
                                           push() → _recent_chunks ring + subscriber queues
                                                                    │
                              new subscriber ──burst-on-connect──► subscriber queue
                              (pre-seeded with ~4 s of recent audio for instant sync)
─────────────────────────────────────────────────────────────────────

Shared broadcast
────────────────
The encoder is the SOLE audio source.  Every byte it produces is fanned
out by push() to every active subscriber queue.  A new client connecting
never starts a new ffmpeg process — they receive an empty queue that is
pre-seeded from _recent_chunks (the last ~4 seconds of encoded audio),
so their browser can sync and start playback immediately at the live
position rather than waiting silently for the next chunk.

Proxy sessions (seamless channel switching)
────────────────────────────────────────────
A _ProxySession wraps a subscriber queue with a stable ID.  The browser
opens one persistent HTTP connection to /stations/sessions/{session_id}
and never changes audio.src.  To switch channels the browser POSTs to
/stations/sessions/{session_id}/station — the server atomically
re-subscribes the session queue to the new station's broadcast, drains
stale audio, and seeds the queue with the new station's burst buffer.
The audio element hears a brief crossfade-quality splice rather than a
reconnection gap.

Metadata sync (drain wait)
──────────────────────────
_pipe_url() returns as soon as the decoder has finished sending PCM to
the encoder stdin.  At that point the encoder still holds several seconds
of audio in its internal buffer.  To keep /now-playing aligned with what
listeners actually hear, the loop SLEEPS for the remaining wall-clock
duration (track.duration_seconds minus elapsed_since_start) before
calling station.advance().  A skip_event during this drain window exits
immediately, preserving snappy skip behaviour.

Encoder crash guard (progressive back-off)
────────────────────────────────────────────
If the encoder dies (BrokenPipeError, ConnectionResetError, or any
crash), _encoder_dead() catches it.  The loop does NOT advance the queue.
Instead it sleeps for min(1 s × consecutive_errors, 30 s) before calling
_ensure_encoder() to start a fresh encoder + reader_task.  The same
track replays from the beginning, which is the safe recovery choice.

Skip isolation
──────────────
POST /skip sets p.skip_event only.  The loop detects it, clears it,
bypasses the drain wait, and advances exactly ONE track — no restarts,
no encoder interaction, no crash path.
"""

import asyncio
import logging
import time
from collections import deque
from typing import Optional, AsyncGenerator
from uuid import uuid4

from ..models.station import StationStatus
from ..services.station_manager import station_manager
from ..services.jellyfin import jellyfin
from ..services import tts_manager
from ..services.banter import get_banter

logger = logging.getLogger(__name__)

# ── Audio constants ───────────────────────────────────────────────────────────
_RATE     = 44100
_CHANNELS = 2
_PCM_FMT  = "s16le"
_BITRATE  = "256k"
_CHUNK    = 8192          # bytes per read from encoder stdout
_Q_MAX    = 512           # per-subscriber queue depth

# Burst-on-connect: number of recent chunks pre-seeded into a new subscriber's
# queue.  16 chunks × 8 192 bytes ≈ 131 KB ≈ 4 seconds at 256 kbps.
# This lets the browser sync and start playback at the live position immediately.
_BURST_CHUNKS = 16

# Error recovery back-off
_BACKOFF_BASE = 1.0   # seconds per consecutive failure
_BACKOFF_MAX  = 30.0  # hard ceiling


# ── ffmpeg command builders ───────────────────────────────────────────────────

def _enc_cmd() -> list[str]:
    """Long-lived CBR MP3 encoder: raw PCM stdin → MP3 stdout.

    -re            read input at native frame rate (1× real-time).  Without
                   this flag ffmpeg encodes as fast as the CPU allows, blasting
                   an entire track's worth of MP3 chunks into the subscriber
                   queues in seconds.  -re creates natural backpressure so
                   chunks trickle out second-by-second, keeping _recent_chunks
                   a true ~4-second live window rather than a stale snapshot
                   of audio that played minutes ago.
    -reservoir 0   disables the bit reservoir so every frame is exactly the
                   same size.  This makes mid-stream MP3 sync trivial for
                   browsers joining an in-progress broadcast.
    """
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-re",
        "-f", _PCM_FMT, "-ar", str(_RATE), "-ac", str(_CHANNELS), "-i", "pipe:0",
        "-c:a", "libmp3lame", "-b:a", _BITRATE,
        "-reservoir", "0",
        "-f", "mp3", "pipe:1",
    ]


def _dec_url_cmd(url: str) -> list[str]:
    """Per-track decoder: Jellyfin URL → raw PCM stdout.

    No -re flag: the encoder already has -re for real-time pacing. Adding -re
    to the decoder is redundant and causes timing fragility on loaded systems
    (CPU busy → decoder stalls → pipe fills → write errors). The network
    stream itself is already real-time, so -re adds no value here.
    """
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", url,
        "-vn", "-f", _PCM_FMT, "-ar", str(_RATE), "-ac", str(_CHANNELS), "pipe:1",
    ]


def _dec_wav_cmd() -> list[str]:
    """Per-announcement decoder: WAV bytes stdin → raw PCM stdout.

    No -re flag: WAV announcements are small (<5s) and are fed all at once
    via communicate(). The -re flag is irrelevant for in-memory input and
    only adds unnecessary latency.
    """
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", "pipe:0",
        "-vn", "-f", _PCM_FMT, "-ar", str(_RATE), "-ac", str(_CHANNELS), "pipe:1",
    ]


# ── Per-client proxy session ──────────────────────────────────────────────────

class _ProxySession:
    """Wraps a subscriber queue with a stable ID for seamless channel switching.

    The browser opens /stations/sessions/{id} once and keeps audio.src fixed.
    Switching stations re-subscribes this queue to a different _Player without
    ever closing the HTTP response.
    """
    def __init__(self, session_id: str, station_id: str):
        self.id         = session_id
        self.station_id = station_id
        self.queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue(maxsize=_Q_MAX)


# ── Per-station player state ──────────────────────────────────────────────────

class _Player:
    def __init__(self, station_id: str):
        self.station_id    = station_id
        self.skip_event    = asyncio.Event()
        self.stop_event    = asyncio.Event()
        self.encoder_proc: Optional[asyncio.subprocess.Process] = None
        self.task:         Optional[asyncio.Task] = None
        self.reader_task:  Optional[asyncio.Task] = None
        self.track_started_at: Optional[float] = None

        # Active subscriber queues — one per connected HTTP client.
        # The broadcast worker is the sole producer; subscribe() is the consumer.
        self.subscribers: list[asyncio.Queue] = []

        # Ring buffer of recent encoded MP3 chunks used for burst-on-connect.
        # New subscribers are pre-seeded from this buffer so their browser can
        # sync to the live position without waiting silently for the next push.
        self._recent_chunks: deque[bytes] = deque(maxlen=_BURST_CHUNKS)

        # Consecutive encoder-crash counter — drives back-off sleep duration
        self.consecutive_errors: int = 0

    def push(self, chunk: bytes) -> None:
        """Fan out one encoded MP3 chunk to every active subscriber.

        The chunk is also appended to _recent_chunks so future subscribers
        receive it as part of their burst-on-connect seed.
        Laggy subscribers whose queue is full are silently evicted.
        """
        self._recent_chunks.append(chunk)
        dead: list[asyncio.Queue] = []
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
        """Send None sentinels to all subscribers and clear the list."""
        for q in self.subscribers:
            try:
                q.put_nowait(None)
            except Exception:
                pass
        self.subscribers.clear()

    def _seed_queue(self, q: asyncio.Queue) -> None:
        """Pre-fill q with the burst buffer for immediate live-position sync."""
        for chunk in list(self._recent_chunks):
            try:
                q.put_nowait(chunk)
            except asyncio.QueueFull:
                break


# ── Service ───────────────────────────────────────────────────────────────────

class PlaybackService:

    def __init__(self):
        self._players:  dict[str, _Player]       = {}
        self._sessions: dict[str, _ProxySession] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self, station_id: str) -> None:
        """Start the always-on broadcast worker for *station_id*.
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
        # Close every proxy session attached to this station first so their
        # HTTP responses end cleanly before the player is torn down.
        for sess_id in [s for s, sess in self._sessions.items()
                        if sess.station_id == station_id]:
            self._close_session_internal(sess_id)

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

    # ── Public controls ───────────────────────────────────────────────────────

    def skip(self, station_id: str) -> None:
        """Signal the broadcast worker to advance one track cleanly.
        Sets skip_event only — never touches the encoder or causes a restart."""
        p = self._players.get(station_id)
        if p:
            p.skip_event.set()

    def is_running(self, station_id: str) -> bool:
        return station_id in self._players

    def get_elapsed(self, station_id: str) -> Optional[float]:
        p = self._players.get(station_id)
        if p and p.track_started_at is not None:
            station = station_manager.get_station(station_id)
            elapsed = time.time() - p.track_started_at
            # Clamp to track duration to prevent progress bar from wrapping
            if station and station.current_track:
                duration = station.current_track.duration_seconds
                if duration:
                    return min(elapsed, duration)
            return elapsed
        return None

    async def subscribe(self, station_id: str) -> AsyncGenerator[bytes, None]:
        """Attach a new HTTP client directly to the live broadcast stream.

        The subscriber queue is pre-seeded with the last ~4 seconds of encoded
        audio (burst-on-connect) so the browser can sync immediately to the
        live position rather than buffering silently.

        Connecting or disconnecting a client has zero effect on the background
        broadcast worker.
        """
        p = self._players.get(station_id)
        if not p:
            return

        q: asyncio.Queue = asyncio.Queue(maxsize=_Q_MAX)
        p._seed_queue(q)
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

    # ── Proxy session management ──────────────────────────────────────────────

    def create_session(self, station_id: str) -> Optional[str]:
        """Create a persistent proxy session pre-subscribed to *station_id*.

        Returns a session ID that the browser embeds in its audio.src URL once.
        The URL never changes — channel switching is handled server-side via
        switch_session().
        """
        p = self._players.get(station_id)
        if not p:
            return None
        sess_id = uuid4().hex[:16]
        sess = _ProxySession(sess_id, station_id)
        p._seed_queue(sess.queue)
        p.subscribers.append(sess.queue)
        self._sessions[sess_id] = sess
        logger.debug("Session %s created for station %s", sess_id, station_id)
        return sess_id

    def switch_session(self, session_id: str, new_station_id: str) -> bool:
        """Re-subscribe a proxy session to a different station without closing
        the HTTP connection.  The old station's burst buffer is drained and the
        new station's burst buffer is seeded atomically."""
        sess = self._sessions.get(session_id)
        if not sess:
            return False
        new_p = self._players.get(new_station_id)
        if not new_p:
            return False

        # Unsubscribe from old station
        old_p = self._players.get(sess.station_id)
        if old_p:
            try:
                old_p.subscribers.remove(sess.queue)
            except ValueError:
                pass

        # Drain stale audio so the new station starts cleanly
        drained = 0
        while not sess.queue.empty():
            try:
                sess.queue.get_nowait()
                drained += 1
            except asyncio.QueueEmpty:
                break

        # Seed with the new station's burst buffer
        new_p._seed_queue(sess.queue)
        sess.station_id = new_station_id
        new_p.subscribers.append(sess.queue)

        logger.debug(
            "Session %s switched to station %s (drained %d stale chunks)",
            session_id, new_station_id, drained,
        )
        return True

    async def stream_session(self, session_id: str) -> AsyncGenerator[bytes, None]:
        """Yield encoded MP3 chunks for an existing proxy session.

        This generator runs for the lifetime of the HTTP connection.  The
        session queue is populated by whatever station the session is currently
        subscribed to; switch_session() atomically redirects it mid-stream.
        """
        sess = self._sessions.get(session_id)
        if not sess:
            return
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(sess.queue.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    if session_id not in self._sessions:
                        break
                    continue
                if chunk is None:
                    break
                yield chunk
        except asyncio.CancelledError:
            pass
        finally:
            self._close_session_internal(session_id)

    def _close_session_internal(self, session_id: str) -> None:
        """Remove the session from the registry and unsubscribe its queue.
        Sends a None sentinel so stream_session() exits if still running."""
        sess = self._sessions.pop(session_id, None)
        if not sess:
            return
        p = self._players.get(sess.station_id)
        if p:
            try:
                p.subscribers.remove(sess.queue)
            except ValueError:
                pass
        try:
            sess.queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        logger.debug("Session %s closed", session_id)

    def session_station(self, session_id: str) -> Optional[str]:
        """Return the station_id a proxy session is currently subscribed to."""
        sess = self._sessions.get(session_id)
        return sess.station_id if sess else None

    # ── Encoder lifecycle helpers ─────────────────────────────────────────────

    def _encoder_dead(self, p: _Player) -> bool:
        """True if the encoder process has exited or was never started."""
        return p.encoder_proc is None or p.encoder_proc.returncode is not None

    async def _ensure_encoder(self, p: _Player, sid: str) -> bool:
        """Start (or restart) the encoder if it has exited.

        Applies a progressive back-off sleep before restarting to prevent
        tight crash loops.  Returns False only if ffmpeg is missing.
        """
        if not self._encoder_dead(p):
            return True

        # Cancel the stale reader task before spawning a fresh encoder
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
            # Respect stop_event during the sleep
            try:
                await asyncio.wait_for(p.stop_event.wait(), timeout=backoff)
                return False  # stop was requested during back-off
            except asyncio.TimeoutError:
                pass

        if p.stop_event.is_set():
            return False

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
        """Continuously read encoded MP3 chunks from the encoder stdout and
        fan them out to every active subscriber queue via push()."""
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

    async def _pipe_wav(self, wav: bytes, enc_in: asyncio.StreamWriter,
                        p: _Player) -> bool:
        """Decode a WAV announcement to raw PCM and feed the encoder stdin.
        Returns True on success, False on skip/stop/pipe error."""
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

    async def _pipe_url(self, url: str, enc_in: asyncio.StreamWriter,
                        p: _Player) -> bool:
        """Decode audio from a Jellyfin URL to raw PCM and feed the encoder stdin.
        Returns True ONLY when the decoder reaches EOF naturally (track finished).
        Returns False on skip, stop, decoder timeout, or pipe error."""
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
            # Initialize prefetch_job at loop start so crash handlers can cancel it
            prefetch_job: Optional[asyncio.Task] = None

            # ── Step 1: ensure the encoder is alive ───────────────────────────
            # The queue is NOT advanced on crash — same track replays.
            if not await self._ensure_encoder(p, sid):
                break  # ffmpeg missing or stop requested during back-off
            enc_in = p.encoder_proc.stdin

            # ── Step 2: resolve current track ─────────────────────────────────
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

            # ── Step 3: TTS announcement ───────────────────────────────────────
            tts = prefetched_tts or await self._tts_for_track(track, station.next_track)
            prefetched_tts = None

            if tts and not p.skip_event.is_set():
                await self._pipe_wav(tts, enc_in, p)
                # If the encoder died while writing the announcement, restart
                # it on the next loop iteration without advancing the queue.
                if self._encoder_dead(p):
                    p.consecutive_errors += 1
                    logger.warning(
                        "[%s] Encoder died during TTS (error #%d) — restarting",
                        sid, p.consecutive_errors,
                    )
                    p.skip_event.clear()
                    # Cancel prefetch to prevent orphaned tasks accumulating
                    if prefetch_job and not prefetch_job.done():
                        prefetch_job.cancel()
                    continue

            p.skip_event.clear()
            if p.stop_event.is_set():
                break

            # ── Step 4: pre-fetch next TTS while this track plays ──────────────
            next_track = station.next_track
            prefetch_job = asyncio.create_task(
                self._tts_for_track(
                    next_track,
                    station.queue[station.current_index + 2]
                    if station.current_index + 2 < len(station.queue) else None,
                )
            ) if next_track else None

            # ── Step 5: stream the music track ────────────────────────────────
            url = jellyfin.stream_url(track.id)
            logger.info("[%s] ▶ %s — %s", sid, track.artist, track.name)
            p.track_started_at = time.time()
            station.status     = StationStatus.playing

            completed = await self._pipe_url(url, enc_in, p)

            # Collect or cancel the pre-fetched TTS
            if prefetch_job:
                if completed and not p.skip_event.is_set():
                    try:
                        prefetched_tts = await asyncio.wait_for(
                            prefetch_job, timeout=3.0
                        )
                    except asyncio.TimeoutError:
                        prefetch_job.cancel()
                else:
                    prefetch_job.cancel()

            # ── Step 6: encoder crash guard ────────────────────────────────────
            # A dead encoder after _pipe_url indicates a pipe/network failure,
            # NOT a user skip.  Back off and restart WITHOUT advancing the queue.
            if self._encoder_dead(p):
                p.consecutive_errors += 1
                logger.warning(
                    "[%s] Encoder died during track (error #%d) — restarting same track",
                    sid, p.consecutive_errors,
                )
                p.skip_event.clear()
                # Cancel prefetch to prevent orphaned tasks accumulating
                if prefetch_job and not prefetch_job.done():
                    prefetch_job.cancel()
                continue

            # ── Step 7: metadata-sync drain wait ──────────────────────────────
            # _pipe_url() returns as soon as the decoder has sent all PCM bytes
            # to the encoder stdin.  The encoder's internal buffer still holds
            # several seconds of audio that hasn't been pushed to subscribers yet.
            #
            # We sleep here for the remaining wall-clock time so that
            # station.advance() (and the /now-playing metadata change) only fires
            # when listeners have actually heard the end of the track.
            #
            # If the user skips during this drain window, skip_event.wait()
            # returns immediately, preserving snappy skip behaviour.
            if completed and not p.skip_event.is_set():
                if track.duration_seconds is not None:
                    elapsed   = time.time() - p.track_started_at
                    remaining = track.duration_seconds - elapsed
                    if 0.5 < remaining <= 60:
                        try:
                            await asyncio.wait_for(
                                p.skip_event.wait(), timeout=remaining
                            )
                            # skip_event fired during drain — proceed to advance
                        except asyncio.TimeoutError:
                            pass  # normal path — duration elapsed naturally

            # ── Step 8: advance queue ──────────────────────────────────────────
            # Only reached when:
            #   (a) encoder is confirmed alive, AND
            #   (b) the track finished naturally (completed=True) with the
            #       encoder buffer fully drained, OR
            #   (c) the user explicitly skipped (skip_event set, encoder alive)
            p.consecutive_errors = 0
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
