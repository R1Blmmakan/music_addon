import logging
import time
import httpx
from typing import AsyncGenerator
from app.providers.base import MusicProvider
from app.crypto.deezer_cipher import (
    get_track_blowfish_key,
    decrypt_stripe_chunk,
    CHUNK_SIZE
)

logger = logging.getLogger("bitchord.deezer")

# Deezer license_token TTL is roughly 3 hours in practice; re-auth every 23h is conservative but safe
_SESSION_TTL_SECONDS = 23 * 3600

class DeezerProvider(MusicProvider):
    def __init__(self, arl: str, public_host: str):
        self.arl = arl
        self.public_host = public_host
        self.client = httpx.AsyncClient(
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=15.0,
            follow_redirects=True,
        )
        self._api_token: str | None = None
        self._license_token: str | None = None
        self._session_initialized_at: float = 0.0

    @property
    def name(self) -> str:
        return "deezer"

    def is_configured(self) -> bool:
        return bool(self.arl and len(self.arl) >= 64)

    def _session_is_stale(self) -> bool:
        if not self._api_token or not self._license_token:
            return True
        return (time.monotonic() - self._session_initialized_at) >= _SESSION_TTL_SECONDS

    async def _init_session(self) -> bool:
        """Authenticate with Deezer gateway using ARL cookie."""
        if not self.is_configured():
            return False

        try:
            url = "https://www.deezer.com/ajax/gw-light.php?method=deezer.getUserData&api_version=1.0&api_token="
            cookies = {"arl": self.arl}
            resp = await self.client.post(url, cookies=cookies)
            if resp.status_code != 200:
                logger.warning(f"Deezer auth failed with status {resp.status_code}")
                return False

            data = resp.json()
            results = data.get("results", {})
            self._api_token = results.get("checkForm")
            user = results.get("USER", {})
            self._license_token = user.get("OPTIONS", {}).get("license_token")

            user_id = user.get("USER_ID", 0)
            if user_id and user_id != 0:
                self._session_initialized_at = time.monotonic()
                logger.info(f"Deezer authenticated as user ID {user_id}")
                return True

            logger.error("Deezer returned invalid user session for provided ARL")
            return False
        except Exception as exc:
            logger.error(f"Error during Deezer session initialization: {exc}")
            return False

    async def _ensure_session(self) -> bool:
        """Re-authenticate if the session is missing or older than _SESSION_TTL_SECONDS."""
        if self._session_is_stale():
            return await self._init_session()
        return True

    async def health(self) -> bool:
        if not self.is_configured():
            return False
        return await self._ensure_session()

    async def search(self, query: str, limit: int = 5) -> list[dict]:
        """Search Deezer public catalogue without requiring authenticated session."""
        try:
            url = "https://api.deezer.com/search"
            resp = await self.client.get(url, params={"q": query, "limit": limit})
            if resp.status_code != 200:
                return []

            items = resp.json().get("data", [])
            tracks = []
            for item in items:
                artwork = (
                    item.get("album", {}).get("cover_xl")
                    or item.get("album", {}).get("cover_big")
                    or item.get("album", {}).get("cover_medium")
                )
                tracks.append({
                    "id": f"dz:{item['id']}",
                    "title": item.get("title", ""),
                    "artist": item.get("artist", {}).get("name", ""),
                    "album": item.get("album", {}).get("title", ""),
                    "duration": float(item.get("duration", 0)),
                    "artworkURL": artwork,
                    "format": "flac",
                    "audioQuality": "LOSSLESS",
                    "bitrate": 1411,
                })
            return tracks
        except Exception as exc:
            logger.error(f"Deezer search error for '{query}': {exc}")
            return []

    async def get_track_data(self, track_id: str) -> dict | None:
        """Fetch internal song details from Deezer gateway."""
        if not await self._ensure_session():
            return None

        url = f"https://www.deezer.com/ajax/gw-light.php?method=song.getData&api_version=1.0&api_token={self._api_token}"
        cookies = {"arl": self.arl}
        try:
            resp = await self.client.post(url, cookies=cookies, json={"sng_id": track_id})
            data = resp.json()
            return data.get("results")
        except Exception as exc:
            logger.error(f"Failed to fetch song data for {track_id}: {exc}")
            return None

    async def get_cdn_url(self, track_id: str, quality: str = "lossless") -> tuple[str | None, str]:
        """Request media delivery stream URL from Deezer media API."""
        song_data = await self.get_track_data(track_id)
        if not song_data:
            return None, "flac"

        track_token = song_data.get("TRACK_TOKEN")
        if not track_token or not self._license_token:
            return None, "flac"

        url = "https://media.deezer.com/v1/get_url"
        # Always request FLAC first; MP3 fallback only if Deezer has no FLAC for this track
        requested_formats = [
            {"cipher": "BF_CBC_STRIPE", "format": "FLAC"},
            {"cipher": "BF_CBC_STRIPE", "format": "MP3_320"},
            {"cipher": "BF_CBC_STRIPE", "format": "MP3_128"},
        ]
        if quality.lower() in ("high", "320"):
            requested_formats = [
                {"cipher": "BF_CBC_STRIPE", "format": "MP3_320"},
                {"cipher": "BF_CBC_STRIPE", "format": "FLAC"},
            ]

        payload = {
            "license_token": self._license_token,
            "media": [{"type": "FULL", "formats": requested_formats}],
            "track_tokens": [track_token],
        }

        try:
            resp = await self.client.post(url, json=payload)
            data = resp.json()
            media_list = data.get("data", [{}])[0].get("media", [])
            if not media_list:
                return None, "flac"

            first_media = media_list[0]
            sources = first_media.get("sources", [])
            chosen_format = first_media.get("format", "FLAC").lower()
            if sources:
                return sources[0].get("url"), chosen_format
            return None, chosen_format
        except Exception as exc:
            logger.error(f"Deezer media get_url error: {exc}")
            return None, "flac"

    async def get_stream(self, track_id: str, quality: str = "lossless") -> dict | None:
        """Return BitChord stream response pointing to internal decrypt proxy."""
        clean_id = track_id.replace("dz:", "")
        cdn_url, audio_format = await self.get_cdn_url(clean_id, quality)
        if not cdn_url:
            return None

        # Reject non-FLAC when caller expects lossless; BitChord will fall back to YouTube otherwise
        is_flac = audio_format == "flac"
        if not is_flac and quality == "lossless":
            logger.info(f"Deezer track {clean_id} has no FLAC; returning None to trigger provider fallback")
            return None

        return {
            "url": f"{self.public_host}/audio/dz/{clean_id}",
            "format": "flac" if is_flac else "mp3",
            "codec": "flac" if is_flac else "mp3",
            "container": "flac" if is_flac else "mp3",
            "bitDepth": 16,
            "sampleRate": 44100,
            "bitrate": 1411 if is_flac else 320,
        }

    async def stream_decrypted_audio(
        self,
        track_id: str,
        range_header: str | None = None
    ) -> tuple[AsyncGenerator[bytes, None], dict[str, str], int]:
        """Fetch encrypted CDN stream, decrypt Blowfish chunks, and yield clean audio."""
        cdn_url, audio_format = await self.get_cdn_url(track_id)
        if not cdn_url:
            raise RuntimeError(f"Unable to retrieve stream CDN URL for track {track_id}")

        key = get_track_blowfish_key(track_id)
        req_headers = {}
        if range_header:
            req_headers["Range"] = range_header

        cdn_resp = await self.client.get(cdn_url, headers=req_headers)
        status_code = cdn_resp.status_code

        response_headers = {
            "Content-Type": "audio/flac" if audio_format == "flac" else "audio/mpeg",
            "Accept-Ranges": "bytes",
        }
        for hdr in ("Content-Length", "Content-Range"):
            if hdr in cdn_resp.headers:
                response_headers[hdr] = cdn_resp.headers[hdr]

        async def audio_generator() -> AsyncGenerator[bytes, None]:
            chunk_idx = 0
            buffer = bytearray()
            async for raw_bytes in cdn_resp.aiter_bytes():
                buffer.extend(raw_bytes)
                while len(buffer) >= CHUNK_SIZE:
                    block = bytes(buffer[:CHUNK_SIZE])
                    del buffer[:CHUNK_SIZE]

                    # Every third 2048-byte stripe in Deezer's encryption scheme is Blowfish-CBC encrypted
                    if chunk_idx % 3 == 0:
                        decrypted = decrypt_stripe_chunk(block, key)
                        yield decrypted
                    else:
                        yield block
                    chunk_idx += 1

            # Trailing bytes shorter than CHUNK_SIZE are always plaintext
            if buffer:
                yield bytes(buffer)

        return audio_generator(), response_headers, status_code
