"""
Audio playback service.

One asyncio Task per station runs a loop:
  1. Announce current + next track via TTS (with a banter line).
  2. Play the audio stream through VLC.
  3. Poll until VLC reports end-of-media (or a skip/stop signal arrives).
  4. Advance the queue and repeat.

Skip and pause are signalled via per-station asyncio.Event objects so the loop
reacts immediately without blocking.

VLC plays the remote HTTP stream directly — no local buffering needed.
"""
import asyncio
import logging
from typing import Optional

import vlc

from ..models.station import StationStatus
from ..services.station_manager import station_manager
from ..services.jellyfin import jellyfin
from ..services.tts import speak, build_track_announcement
from ..services.banter import get_banter

logger = logging.getLogger(__name__)

# How often (seconds) we poll VLC for end-of-media
_POLL_INTERVAL = 0.5
# How long (seconds) to wait after TTS before audio starts (feels more natural)
_POST_TTS_PAUSE = 0.4


class _StationPlayer:
    """Internal per-station state held by PlaybackService."""

    def __init__(self, station_id: str):
        self.station_id = station_id
        self.skip_event = asyncio.Event()
        self.stop_event = asyncio.Event()
        self.paused = False
        self._vlc_instance: vlc.Instance = vlc.Instance("--no-video", "--quiet")
        self._media_player: Optional[vlc.MediaPlayer] = None
        self.task: Optional[asyncio.Task] = None

    def _new_player(self) -> vlc.MediaPlayer:
        if self._media_player:
            self._media_player.stop()
            self._media_player.release()
        self._media_player = self._vlc_instance.media_player_new()
        return self._media_player

    def stop_audio(self) -> None:
        if self._media_player:
            self._media_player.stop()

    def pause_audio(self) -> None:
        if self._media_player:
            self._media_player.pause()  # VLC pause() is a toggle

    def release(self) -> None:
        if self._media_player:
            self._media_player.stop()
            self._media_player.release()
            self._media_player = None
        self._vlc_instance.release()


class PlaybackService:
    def __init__(self):
        self._players: dict[str, _StationPlayer] = {}

    # ------------------------------------------------------------------
    # Public controls
    # ------------------------------------------------------------------

    async def start(self, station_id: str) -> None:
        """Start the playback loop for a station."""
        if station_id in self._players:
            # Already running — just unpause if paused
            player = self._players[station_id]
            if player.paused:
                player.paused = False
                player.pause_audio()
            return

        player = _StationPlayer(station_id)
        self._players[station_id] = player
        player.task = asyncio.create_task(
            self._playback_loop(player),
            name=f"playback-{station_id}",
        )
        player.task.add_done_callback(lambda t: self._on_task_done(station_id, t))

    def pause(self, station_id: str) -> None:
        player = self._players.get(station_id)
        if player:
            player.paused = not player.paused
            player.pause_audio()

    def skip(self, station_id: str) -> None:
        player = self._players.get(station_id)
        if player:
            player.skip_event.set()

    async def stop(self, station_id: str) -> None:
        player = self._players.get(station_id)
        if player:
            player.stop_event.set()
            player.stop_audio()
            if player.task:
                try:
                    await asyncio.wait_for(player.task, timeout=3.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    player.task.cancel()
            player.release()
            self._players.pop(station_id, None)

    def is_playing(self, station_id: str) -> bool:
        player = self._players.get(station_id)
        return player is not None and not player.paused

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    async def _playback_loop(self, player: _StationPlayer) -> None:
        station_id = player.station_id
        logger.info("Playback loop started for station %s", station_id)

        while not player.stop_event.is_set():
            station = station_manager.get_station(station_id)
            if station is None:
                logger.warning("Station %s disappeared; stopping loop", station_id)
                break

            track = station.current_track
            if track is None:
                logger.info("Queue exhausted for station %s; refilling", station_id)
                try:
                    station = await station_manager.refill_queue(station_id)
                    track = station.current_track
                except Exception:
                    logger.exception("Failed to refill queue for station %s", station_id)
                    await asyncio.sleep(5)
                    continue

            if track is None:
                logger.error("Still no tracks after refill; giving up")
                break

            # -- Announce ---------------------------------------------------
            banter = get_banter(track.genre)
            announcement = build_track_announcement(
                artist=track.artist,
                title=track.name,
                next_artist=station.next_track.artist if station.next_track else None,
                next_title=station.next_track.name if station.next_track else None,
                banter=banter,
            )
            logger.info("[%s] Announcing: %s — %s", station_id, track.artist, track.name)
            try:
                await speak(announcement)
                await asyncio.sleep(_POST_TTS_PAUSE)
            except Exception:
                logger.exception("TTS failed; skipping announcement")

            if player.stop_event.is_set():
                break

            # -- Play --------------------------------------------------------
            stream_url = jellyfin.stream_url(track.id)
            logger.info("[%s] Playing: %s", station_id, stream_url)
            station.status = StationStatus.playing

            vlc_player = player._new_player()
            media = player._vlc_instance.media_new(stream_url)
            vlc_player.set_media(media)
            vlc_player.play()

            # Clear skip signal from any previous iteration
            player.skip_event.clear()

            # Wait until: track ends naturally | skip signal | stop signal
            await self._wait_for_end(vlc_player, player)

            vlc_player.stop()

            if player.stop_event.is_set():
                break

            # -- Advance -----------------------------------------------------
            next_track = station.advance()
            if next_track is None:
                # End of queue — loop will refill on next iteration
                logger.info("[%s] End of queue", station_id)

        station = station_manager.get_station(station_id)
        if station:
            station.status = StationStatus.idle
        logger.info("Playback loop ended for station %s", station_id)

    async def _wait_for_end(self, vlc_player: vlc.MediaPlayer, player: _StationPlayer) -> None:
        """Poll until the track ends, is skipped, or stop is signalled."""
        while True:
            if player.stop_event.is_set() or player.skip_event.is_set():
                return

            state = vlc_player.get_state()
            if state in (vlc.State.Ended, vlc.State.Error, vlc.State.Stopped):
                return

            await asyncio.sleep(_POLL_INTERVAL)

    def _on_task_done(self, station_id: str, task: asyncio.Task) -> None:
        exc = task.exception() if not task.cancelled() else None
        if exc:
            logger.error("Playback task for station %s crashed: %s", station_id, exc, exc_info=exc)
        self._players.pop(station_id, None)


playback_service = PlaybackService()
