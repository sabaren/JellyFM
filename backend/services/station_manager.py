"""In-memory station registry. Replace with a DB-backed store later if needed."""
import random
from typing import Optional

from ..models.station import Station, StationStatus
from ..models.jellyfin import Track
from .jellyfin import jellyfin


class StationManager:
    def __init__(self):
        self._stations: dict[str, Station] = {}

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def create_station(self, name: str, genre: str, shuffle: bool = True) -> Station:
        tracks = await jellyfin.get_tracks_by_genre(genre)
        if shuffle:
            random.shuffle(tracks)
        station = Station(name=name, genre=genre, queue=tracks, shuffle=shuffle)
        self._stations[station.id] = station
        return station

    def get_station(self, station_id: str) -> Optional[Station]:
        return self._stations.get(station_id)

    def list_stations(self) -> list[Station]:
        return list(self._stations.values())

    def delete_station(self, station_id: str) -> bool:
        return self._stations.pop(station_id, None) is not None

    # ------------------------------------------------------------------
    # Playback controls (queue logic only — no audio yet)
    # ------------------------------------------------------------------

    def play(self, station_id: str) -> Optional[Track]:
        station = self._get_or_raise(station_id)
        station.status = StationStatus.playing
        return station.current_track

    def pause(self, station_id: str) -> None:
        station = self._get_or_raise(station_id)
        station.status = StationStatus.paused

    def skip(self, station_id: str) -> Optional[Track]:
        station = self._get_or_raise(station_id)
        track = station.advance()
        if track is None:
            station.status = StationStatus.idle
        return track

    def previous(self, station_id: str) -> Optional[Track]:
        station = self._get_or_raise(station_id)
        return station.rewind()

    async def refill_queue(self, station_id: str) -> Station:
        """Re-fetch tracks from Jellyfin and reset the queue."""
        station = self._get_or_raise(station_id)
        tracks = await jellyfin.get_tracks_by_genre(station.genre)
        if station.shuffle:
            random.shuffle(tracks)
        station.queue = tracks
        station.current_index = 0
        station.status = StationStatus.idle
        return station

    # ------------------------------------------------------------------

    def _get_or_raise(self, station_id: str) -> Station:
        station = self._stations.get(station_id)
        if station is None:
            raise KeyError(f"Station {station_id!r} not found")
        return station


station_manager = StationManager()
