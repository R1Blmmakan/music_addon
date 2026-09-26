"""
Native Tidal Music Provider
Communicates directly with Tidal's official API for search and lossless FLAC playback.
Handles automatic OAuth token refresh without requiring external microservices.
"""
import asyncio
import base64
import json
import logging
import os
from pathlib import Path
import httpx
from app.providers.base import MusicProvider

logger = logging.getLogger("bitchord.tidal")

TOKEN_URL = "https://auth.tidal.com/v1/oauth2/token"
API_BASE = "https://api.tidal.com/v1"

class TidalProvider(MusicProvider):
    def __init__(self, token_file: str = "token.json", country_code: str = "US", public_host: str = ""):
        self.token_file = Path(token_file)
        self.country_code = country_code or "US"
        self.public_host = public_host.rstrip("/") if public_host else ""
        self.client = httpx.AsyncClient(timeout=15.0)
        self.token_data: dict | None = None
        self.dash_cache: dict[str, str] = {}
        self._load_token()

    @property
    def name(self) -> str:
        return "tidal"

    def _load_token(self):
        """Load stored OAuth token from local disk."""
        if self.token_file.exists():
            try:
                with open(self.token_file, "r", encoding="utf-8") as f:
                    self.token_data = json.load(f)
            except Exception as e:
                logger.error(f"Failed to read Tidal token file {self.token_file}: {e}")
                self.token_data = None
        else:
            self.token_data = None

    def is_configured(self) -> bool:
        """Check if Tidal OAuth credentials are present."""
        if not self.token_data:
            self._load_token()
        return bool(self.token_data and self.token_data.get("access_token"))

    async def _refresh_access_token(self) -> bool:
        """Refresh expired access token using stored refresh token."""
        if not self.token_data or not self.token_data.get("refresh_token"):
            return False

        client_id = self.token_data.get("client_id", "fX2JxdmntZWK0ixT")
        client_secret = self.token_data.get("client_secret", "1Nm5AfDAjxrgJFJbKNWLeAyKGVGmINuXPPLHVXAvxAg=")
        refresh_token = self.token_data["refresh_token"]

        data = {
            "client_id": client_id,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
            "scope": "r_usr+w_usr+w_sub",
        }
        auth = (client_id, client_secret)

        try:
            resp = await self.client.post(TOKEN_URL, data=data, auth=auth)
            if resp.status_code == 200:
                new_info = resp.json()
                self.token_data["access_token"] = new_info["access_token"]
                if "refresh_token" in new_info:
                    self.token_data["refresh_token"] = new_info["refresh_token"]

                with open(self.token_file, "w", encoding="utf-8") as f:
                    json.dump(self.token_data, f, indent=2)

                logger.info("Successfully refreshed Tidal access token.")
                return True
            else:
                logger.error(f"Tidal token refresh failed with HTTP {resp.status_code}: {resp.text}")
                return False
        except Exception as exc:
            logger.error(f"Exception during Tidal token refresh: {exc}")
            return False

    def _auth_headers(self) -> dict:
        access_token = self.token_data.get("access_token", "") if self.token_data else ""
        return {
            "authorization": f"Bearer {access_token}",
            "User-Agent": "okhttp/5.3.2",
            "Accept": "application/json",
            "X-Platform": "android",
            "X-Tidal-Platform": "android",
        }

    async def health(self) -> bool:
        """Verify Tidal API accessibility and token validity."""
        if not self.is_configured():
            return False
        try:
            url = f"{API_BASE}/sessions"
            resp = await self.client.get(url, headers=self._auth_headers())
            if resp.status_code == 401:
                refreshed = await self._refresh_access_token()
                if refreshed:
                    resp = await self.client.get(url, headers=self._auth_headers())
            return resp.status_code == 200
        except Exception:
            return False

    async def search(self, query: str, limit: int = 5) -> list[dict]:
        """Search Tidal catalogue for tracks."""
        if not self.is_configured():
            return []

        clean_query = query.strip()
        if not clean_query:
            return []

        url = f"{API_BASE}/search/tracks"
        params = {
            "query": clean_query,
            "limit": limit,
            "offset": 0,
            "countryCode": self.country_code,
        }

        try:
            resp = await self.client.get(url, headers=self._auth_headers(), params=params)
            if resp.status_code == 401:
                if await self._refresh_access_token():
                    resp = await self.client.get(url, headers=self._auth_headers(), params=params)

            if resp.status_code != 200:
                logger.error(f"Tidal search error ({resp.status_code}): {resp.text[:200]}")
                return []

            data = resp.json()
            items = data.get("items", [])

            tracks = []
            for item in items[:limit]:
                tags = item.get("mediaMetadata", {}).get("tags", [])
                is_hi_res = "HI_RES_LOSSLESS" in tags or item.get("audioQuality") == "HI_RES_LOSSLESS"
                is_atmos = "DOLBY_ATMOS" in tags or "DOLBY_ATMOS" in item.get("audioModes", [])

                cover_hash = item.get("album", {}).get("cover")
                artwork = None
                if cover_hash:
                    artwork = f"https://resources.tidal.com/images/{cover_hash.replace('-', '/')}/1280x1280.jpg"

                tracks.append({
                    "id": f"td:{item['id']}",
                    "title": item.get("title", ""),
                    "artist": item.get("artist", {}).get("name", ""),
                    "album": item.get("album", {}).get("title", ""),
                    "duration": float(item.get("duration", 0)),
                    "artworkURL": artwork,
                    "format": "flac",
                    "audioQuality": "HI_RES_LOSSLESS" if is_hi_res else "LOSSLESS",
                    "bitrate": 3000 if is_hi_res else 1411,
                    "audioModes": ["DOLBY_ATMOS"] if is_atmos else ["STEREO"],
                    "atmos": is_atmos,
                })
            return tracks
        except Exception as exc:
            logger.error(f"Tidal search failed for '{query}': {exc}")
            return []

    async def get_stream(self, track_id: str, quality: str = "lossless") -> dict | None:
        """Resolve track ID into signed FLAC CDN URL or DASH manifest."""
        if not self.is_configured():
            return None

        clean_id = track_id.replace("td:", "")
        wanted_quality = "HI_RES_LOSSLESS" if quality.lower() in ("lossless", "max", "hi-res") else "LOSSLESS"

        qualities_to_try = [wanted_quality]
        if wanted_quality != "LOSSLESS":
            qualities_to_try.append("LOSSLESS")

        data = None
        for q in qualities_to_try:
            for endpoint in ("playbackinfopostpaywall", "playbackinfo"):
                url = f"{API_BASE}/tracks/{clean_id}/{endpoint}"
                params = {
                    "countryCode": self.country_code,
                    "audioquality": q,
                    "playbackmode": "STREAM",
                    "assetpresentation": "FULL",
                }
                try:
                    resp = await self.client.get(url, headers=self._auth_headers(), params=params)
                    if resp.status_code == 401:
                        if await self._refresh_access_token():
                            resp = await self.client.get(url, headers=self._auth_headers(), params=params)

                    if resp.status_code == 200:
                        data = resp.json()
                        break
                except Exception:
                    pass
            if data:
                break

        if not data:
            logger.error(f"Tidal playbackinfo failed for track {clean_id} across all quality tiers")
            return None

        try:
            raw_manifest = data.get("manifest")
            mime = data.get("manifestMimeType", "")
            bit_depth = data.get("bitDepth", 16)
            sample_rate = data.get("sampleRate", 44100)

            # Tidal BTS payload contains base64 encoded JSON with direct CDN URLs
            if "bts" in mime and raw_manifest:
                try:
                    decoded = json.loads(base64.b64decode(raw_manifest).decode("utf-8"))
                    direct_urls = decoded.get("urls", [])
                    if direct_urls:
                        return {
                            "url": direct_urls[0],
                            "format": "flac",
                            "codec": "flac",
                            "container": "flac",
                            "bitDepth": bit_depth,
                            "sampleRate": sample_rate,
                            "bitrate": 1411 if bit_depth == 16 else 3000,
                        }
                except Exception as decode_err:
                    logger.error(f"Failed to decode Tidal BTS manifest: {decode_err}")

            # Tidal DASH manifest payload: serve via proper HTTP endpoint instead of unplayable data URI
            if "dash" in mime and raw_manifest:
                try:
                    decoded_xml = base64.b64decode(raw_manifest).decode("utf-8")
                    self.dash_cache[clean_id] = decoded_xml
                except Exception as e:
                    logger.error(f"Failed to decode DASH XML: {e}")

                manifest_url = f"{self.public_host}/dash/td/{clean_id}.mpd" if self.public_host else f"/dash/td/{clean_id}.mpd"
                return {
                    "url": manifest_url,
                    "format": "dash",
                    "codec": "flac",
                    "container": "dash",
                    "bitDepth": bit_depth,
                    "sampleRate": sample_rate,
                    "bitrate": 2000,
                }

            return None
        except Exception as exc:
            logger.error(f"Tidal stream resolution failed for {clean_id}: {exc}")
            return None