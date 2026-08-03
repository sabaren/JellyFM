import logging
import httpx
import random
from typing import Optional

from ..config import settings
from ..models.jellyfin import Genre, Track
from .romanize import romanize

logger = logging.getLogger(__name__)

# Jellyfin uses this header to identify the client
_AUTH_HEADER_TEMPLATE = (
    'MediaBrowser Client="JellyFM", Device="JellyFM-Server", '
    'DeviceId="jellyfm-backend-001", Version="1.0.0"{token_part}'
)


def _auth_header(token: Optional[str] = None) -> str:
    token_part = f', Token="{token}"' if token else ""
    return _AUTH_HEADER_TEMPLATE.format(token_part=token_part)


class JellyfinClient:
    def __init__(self):
        self._token: str = settings.jellyfin_token
        self._user_id: str = settings.jellyfin_user_id
        self._base = settings.jellyfin_url.rstrip("/")

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    async def authenticate(self) -> None:
        """Obtain an access token using username + password."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base}/Users/AuthenticateByName",
                json={"Username": settings.jellyfin_username, "Pw": settings.jellyfin_password},
                headers={"X-Emby-Authorization": _auth_header(), "Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
            self._token = data["AccessToken"]
            self._user_id = data["User"]["Id"]

    @property
    def is_authenticated(self) -> bool:
        return bool(self._token and self._user_id)

    async def ensure_auth(self) -> None:
        if not self.is_authenticated:
            await self.authenticate()

    # ------------------------------------------------------------------
    # Genres
    # ------------------------------------------------------------------

    async def get_genres(self) -> list[Genre]:
        await self.ensure_auth()
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self._base}/MusicGenres",
                params={
                    "userId": self._user_id,
                    "includeItemTypes": "Audio",
                    "fields": "ItemCounts",
                },
                headers={"X-Emby-Authorization": _auth_header(self._token)},
            )
            resp.raise_for_status()
            items = resp.json().get("Items", [])
            return [
                Genre(
                    id=item["Id"],
                    name=item["Name"],
                    track_count=item.get("ChildCount"),
                )
                for item in items
            ]

    # ------------------------------------------------------------------
    # Tracks
    # ------------------------------------------------------------------

    async def get_tracks_by_genre(self, genre_name: str, limit: int = 200) -> list[Track]:
        """Fetch tracks for a genre, following pagination until all are retrieved.
        
        Refreshes auth token on 401 responses.
        """
        await self.ensure_auth()
        
        tracks: list[Track] = []
        start_index = 0
        PAGE_SIZE = 100  # Fetch in batches to reduce memory and timeout risk
        
        while len(tracks) < limit:
            remaining = limit - len(tracks)
            page_limit = min(PAGE_SIZE, remaining)
            
            async with httpx.AsyncClient() as client:
                try:
                    resp = await client.get(
                        f"{self._base}/Users/{self._user_id}/Items",
                        params={
                            "includeItemTypes": "Audio",
                            "genres": genre_name,
                            "recursive": "true",
                            "fields": "Genres,RunTimeTicks",
                            "startIndex": start_index,
                            "limit": page_limit,
                            "sortBy": "Random",
                        },
                        headers={"X-Emby-Authorization": _auth_header(self._token)},
                    )
                    
                    # Handle token expiry with re-auth
                    if resp.status_code == 401:
                        logger.warning("Jellyfin token expired — re-authenticating")
                        await self.authenticate()
                        resp = await client.get(
                            f"{self._base}/Users/{self._user_id}/Items",
                            params={
                                "includeItemTypes": "Audio",
                                "genres": genre_name,
                                "recursive": "true",
                                "fields": "Genres,RunTimeTicks",
                                "startIndex": start_index,
                                "limit": page_limit,
                                "sortBy": "Random",
                            },
                            headers={"X-Emby-Authorization": _auth_header(self._token)},
                        )
                    
                    resp.raise_for_status()
                    items = resp.json().get("Items", [])
                    
                except httpx.HTTPStatusError as e:
                    logger.exception("Jellyfin API error fetching tracks for genre %r: %s", genre_name, e)
                    break
                
                if not items:
                    break
                
                for item in items:
                    name = item["Name"]
                    artist = item.get("AlbumArtist") or (item.get("Artists") or ["Unknown"])[0]
                    tracks.append(Track(
                        id=item["Id"],
                        name=name,
                        artist=artist,
                        album=item.get("Album"),
                        genre=genre_name,
                        duration_ticks=item.get("RunTimeTicks"),
                        tts_name=romanize(name),
                        tts_artist=romanize(artist),
                    ))
                
                start_index += len(items)
                # If we got fewer items than requested, there are no more
                if len(items) < page_limit:
                    break
        
        logger.info("Fetched %d tracks for genre %r", len(tracks), genre_name)
        return tracks

    def stream_url(self, track_id: str) -> str:
        return f"{self._base}/Audio/{track_id}/stream?static=true&api_key={self._token}"


# Module-level singleton — routers import this directly
jellyfin = JellyfinClient()
