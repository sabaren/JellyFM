from pydantic import BaseModel, Field
from typing import Optional
from enum import Enum
import uuid

from .jellyfin import Track


class StationStatus(str, Enum):
    idle = "idle"
    playing = "playing"
    paused = "paused"


class Station(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    genre: str                      # Jellyfin genre name used to seed this station
    status: StationStatus = StationStatus.idle
    queue: list[Track] = Field(default_factory=list)
    current_index: int = 0
    shuffle: bool = True

    @property
    def current_track(self) -> Optional[Track]:
        if not self.queue or self.current_index >= len(self.queue):
            return None
        return self.queue[self.current_index]

    @property
    def next_track(self) -> Optional[Track]:
        if not self.queue or self.current_index + 1 >= len(self.queue):
            return None
        return self.queue[self.current_index + 1]

    def advance(self) -> Optional[Track]:
        """Move to the next track. Returns the new current track, or None if queue exhausted."""
        if self.current_index + 1 < len(self.queue):
            self.current_index += 1
            return self.current_track
        return None

    def rewind(self) -> Optional[Track]:
        if self.current_index > 0:
            self.current_index -= 1
        return self.current_track
