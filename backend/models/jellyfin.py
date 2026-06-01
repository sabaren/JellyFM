from pydantic import BaseModel
from typing import Optional


class Genre(BaseModel):
    id: str
    name: str
    track_count: Optional[int] = None


class Track(BaseModel):
    id: str
    name: str
    artist: str
    album: Optional[str] = None
    genre: Optional[str] = None
    duration_ticks: Optional[int] = None  # Jellyfin stores duration in 10^-7 s ticks

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.duration_ticks is None:
            return None
        return self.duration_ticks / 10_000_000

    def stream_url(self, base_url: str, token: str) -> str:
        return f"{base_url}/Audio/{self.id}/stream?static=true&api_key={token}"
