import base64
import json
import logging
import httpx
from app.providers.base import MusicProvider

logger = logging.getLogger("bitchord.tidal")

class TidalProvider(MusicProvider):
    def __init__(self, api_url: str):
        self.base_url = api_url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=10.0)

    @property
    def name(self) -> str:
        return "tidal"

    def is_configured(self) -> bool:
        return bool(self.base_url and self.base_url.startswith("http"))

    async def health(self) -> bool:
        if not self.is_configured():
            return False
        try:
            resp = await self.client.get(f"{self.base_url}/")
            return resp.status_code == 200
        except Exception:
            return False

    async def search(self, query: str, limit: int = 5) -> list[dict]:
        """Search Tidal catalogue through upstream hifi-api instance."""
        if not self.is_configured():
            return []

        try:
            resp = await self.client.get(f"{self.base_url}/search/", params={"query": query})
            if resp.status_code != 200:
                resp = await self.client.get(f"{self.base_url}/search", params={"q": query})

            if resp.status_code != 200:
                return []

            data = resp.json().get("data", {})
            items = data.get("items", []) if isinstance(data, dict) else data

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
                    "audioModes": ["DOLBY_ATMOS"] if is_atmos else ["STEREO"],
                    "atmos": is_atmos,
                })
            return tracks
        except Exception as exc:
            logger.error(f"Tidal search failed for '{query}': {exc}")
            return []

    async def get_stream(self, track_id: str, quality: str = "lossless") -> dict | None:
        """Resolve Tidal track ID into direct signed CDN URL or DASH manifest."""
        if not self.is_configured():
            return None

        clean_id = track_id.replace("td:", "")
        wanted_quality = "HI_RES_LOSSLESS" if quality.lower() in ("lossless", "max", "hi-res") else "LOSSLESS"

        try:
            url = f"{self.base_url}/track/"
            resp = await self.client.get(url, params={"id": clean_id, "quality": wanted_quality})
            if resp.status_code != 200:
                return None

            data = resp.json().get("data", {})
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

            # Tidal DASH manifest payload
            if "dash" in mime and raw_manifest:
                # BitChord natively accepts DASH data URIs or direct URLs
                manifest_data_uri = f"data:application/dash+xml;base64,{raw_manifest}"
                return {
                    "url": manifest_data_uri,
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
