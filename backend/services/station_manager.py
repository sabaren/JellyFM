"""In-memory station registry with JSON file persistence."""
import json
import logging
import random
from pathlib import Path
from typing import Optional

from ..models.station import Station, StationStatus
from ..models.jellyfin import Track
from .jellyfin import jellyfin
from .romanize import romanize

logger = logging.getLogger(__name__)

_SAVE_FILE = Path("stations.json")


class StationManager:
    def __init__(self):
        self._stations: dict[str, Station] = {}
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save(self) -> None:
        try:
            data = [s.model_dump() for s in self._stations.values()]
            _SAVE_FILE.write_text(json.dumps(data, indent=2))
        except Exception:
            logger.exception("Failed to save stations")

    def _load(self) -> None:
        if not _SAVE_FILE.exists():
            return
        try:
            data = json.loads(_SAVE_FILE.read_text())
            needs_save = False
            for item in data:
                item["status"] = StationStatus.idle
                station = Station.model_validate(item)
                # Backfill tts_name/tts_artist for tracks saved before romanization
                for track in station.queue:
                    if track.tts_name is None:
                        track.tts_name = romanize(track.name)
                        needs_save = True
                    if track.tts_artist is None:
                        track.tts_artist = romanize(track.artist)
                        needs_save = True
                self._stations[station.id] = station
            logger.info("Loaded %d station(s) from %s", len(self._stations), _SAVE_FILE)
            if needs_save:
                logger.info("Persisting backfilled tts_name/tts_artist to disk")
                self._save()
        except Exception:
            logger.exception("Failed to load stations from %s", _SAVE_FILE)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def create_station(self, name: str, genre: str, shuffle: bool = True) -> Station:
        tracks = await jellyfin.get_tracks_by_genre(genre)
        if shuffle:
            random.shuffle(tracks)
        station = Station(name=name, genre=genre, queue=tracks, shuffle=shuffle)
        self._stations[station.id] = station
        self._save()
        return station

    def get_station(self, station_id: str) -> Optional[Station]:
        return self._stations.get(station_id)

    def list_stations(self) -> list[Station]:
        return list(self._stations.values())

    def delete_station(self, station_id: str) -> bool:
        removed = self._stations.pop(station_id, None) is not None
        if removed:
            self._save()
        return removed

    # ------------------------------------------------------------------
    # Queue controls
    # ------------------------------------------------------------------

    def skip(self, station_id: str) -> Optional[Track]:
        station = self._get_or_raise(station_id)
        return station.advance()

    def previous(self, station_id: str) -> Optional[Track]:
        station = self._get_or_raise(station_id)
        return station.rewind()

    async def refill_queue(self, station_id: str) -> Station:
        station = self._get_or_raise(station_id)
        tracks = await jellyfin.get_tracks_by_genre(station.genre)
        if station.shuffle:
            random.shuffle(tracks)
        station.queue = tracks
        station.current_index = 0
        station.status = StationStatus.idle
        self._save()
        return station

    # ------------------------------------------------------------------

    def _get_or_raise(self, station_id: str) -> Station:
        station = self._stations.get(station_id)
        if station is None:
            raise KeyError(f"Station {station_id!r} not found")
        return station


station_manager = StationManager()
