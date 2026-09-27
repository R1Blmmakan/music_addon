"""
Tidal provider: OAuth token management, search scoring, and FLAC/Hi-Res stream resolution.
Handles BTS (progressive) and DASH manifests. Patches fMP4 init segments for ExoPlayer
because Tidal encodes sampleRate=0 in the fLaC box header for Hi-Res tracks.
"""
import asyncio
import base64
import json
import logging
import re
from collections import OrderedDict
from pathlib import Path
import httpx
from app.providers.base import MusicProvider

logger = logging.getLogger("bitchord.tidal")

TOKEN_URL = "https://auth.tidal.com/v1/oauth2/token"
API_BASE = "https://api.tidal.com/v1"

class _LRUCache:
    """Fixed-capacity LRU dict. Evicts least-recently-used entry when full."""
    def __init__(self, maxsize: int = 256):
        self._maxsize = maxsize
        self._store: OrderedDict = OrderedDict()

    def get(self, key, default=None):
        if key not in self._store:
            return default
        self._store.move_to_end(key)
        return self._store[key]

    def __setitem__(self, key, value):
        if key in self._store:
            self._store.move_to_end(key)
        self._store[key] = value
        if len(self._store) > self._maxsize:
            self._store.popitem(last=False)

    def __contains__(self, key):
        return key in self._store


class TidalProvider(MusicProvider):
    def __init__(self, token_file: str = "token.json", country_code: str = "ID", public_host: str = ""):
        self.token_file = Path(token_file)
        self.configured_country_code = country_code or "ID"
        self.country_code = self.configured_country_code
        self.public_host = public_host.rstrip("/") if public_host else ""
        self.client = httpx.AsyncClient(timeout=15.0)
        self.token_data: dict | None = None
        # Capped at 256 entries each to prevent unbounded RAM growth on long-running containers
        self.dash_cache: _LRUCache = _LRUCache(maxsize=256)
        self.init_url_cache: _LRUCache = _LRUCache(maxsize=256)
        self.init_segment_cache: _LRUCache = _LRUCache(maxsize=256)
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
        if not self.token_data:
            self._load_token()
        return bool(self.token_data and self.token_data.get("access_token"))

    async def _refresh_access_token(self) -> bool:
        """Refresh expired access token using stored refresh token."""
        if not self.token_data or not self.token_data.get("refresh_token"):
            return False

        client_id = self.token_data.get("client_id", "zU4XHVVkc2tDPo4t")
        client_secret = self.token_data.get("client_secret", "VJKhDFqJPqvsPVNBV6ukXTJmwlvbttP7wlMlrc72se4=")
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
            if resp.status_code == 200:
                cc = resp.json().get("countryCode")
                logger.info(f"Tidal session verified (session geoIP: {cc}, active countryCode: {self.country_code})")
                if not self.country_code and cc:
                    self.country_code = cc
                return True
            return False
        except Exception:
            return False

    async def search(self, query: str, limit: int = 5) -> list[dict]:
        """Search Tidal catalogue for tracks with multi-country fallback and smart scoring."""
        if not self.is_configured():
            return []

        clean_query = query.strip()
        if not clean_query:
            return []

        try:
            url = f"{API_BASE}/search/tracks"
            countries_to_try = [self.country_code]
            for fallback_cc in ("ID", "GB", "US"):
                if fallback_cc not in countries_to_try:
                    countries_to_try.append(fallback_cc)

            items = []
            for cc in countries_to_try:
                params = {
                    "query": clean_query,
                    "limit": max(limit * 3, 30),
                    "offset": 0,
                    "countryCode": cc,
                }
                try:
                    resp = await self.client.get(url, headers=self._auth_headers(), params=params)
                    if resp.status_code == 401:
                        if await self._refresh_access_token():
                            resp = await self.client.get(url, headers=self._auth_headers(), params=params)

                    if resp.status_code == 200:
                        data = resp.json()
                        cand_items = data.get("items", [])
                        if cand_items:
                            items = cand_items
                            break
                except Exception as e:
                    logger.warning(f"Tidal search attempt failed for country {cc}: {e}")

            if not items:
                return []

            query_lower = clean_query.lower()
            query_words = [w for w in query_lower.split() if len(w) > 1]

            junk_keywords = (
                "karaoke", "tribute", "originally performed",
                "in the style of", "backing track", "piano tribute",
                "lullaby", "instrumental", "cover", "speed up",
                "sped up", "slowed", "acoustic tribute", "made famous by"
            )

            scored_items = []
            for idx, item in enumerate(items):
                if item.get("type") == "video":
                    continue

                t_title = item.get("title", "").strip().lower()
                t_version = (item.get("version") or "").strip().lower()
                full_title = f"{t_title} {t_version}".strip()
                t_artist = item.get("artist", {}).get("name", "").strip().lower()
                popularity = int(item.get("popularity", 0) or 0)

                # Demote junk unless the query itself requests it (e.g. user searches "slowed")
                is_junk = any(
                    k in full_title or k in t_artist
                    for k in junk_keywords
                    if k not in query_lower
                )

                score = 300 if is_junk else 0

                if t_title == query_lower or full_title == query_lower:
                    score -= 600
                elif full_title.startswith(query_lower):
                    score -= 400
                elif query_lower in full_title:
                    score -= 300
                elif query_words and any(w in t_title for w in query_words):
                    matched_title_words = sum(1 for w in query_words if w in t_title)
                    score -= 150 * matched_title_words
                else:
                    score += 400

                if t_artist == query_lower or query_lower in t_artist:
                    score -= 300
                elif query_words and any(w in t_artist for w in query_words):
                    matched_artist_words = sum(1 for w in query_words if w in t_artist)
                    score -= 100 * matched_artist_words

                # Synergy bonus when query words hit both title and artist
                if len(query_words) >= 2:
                    has_title_match = any(w in t_title for w in query_words)
                    has_artist_match = any(w in t_artist for w in query_words)
                    if has_title_match and has_artist_match:
                        score -= 400

                t_album = item.get("album", {}).get("title", "").strip().lower()
                tags = item.get("mediaMetadata", {}).get("tags", [])
                is_hi_res = "HI_RES_LOSSLESS" in tags or item.get("audioQuality") == "HI_RES_LOSSLESS"
                if is_hi_res:
                    score -= 150
                if "soundtrack" in t_album or "compilation" in t_album:
                    score += 250

                score -= min(popularity, 100)
                scored_items.append((score, idx, item))

            scored_items.sort(key=lambda x: (x[0], x[1]))
            best_items = [x[2] for x in scored_items]

            tracks = []
            for item in best_items:
                audio_modes = item.get("audioModes", [])
                is_atmos = "DOLBY_ATMOS" in audio_modes
                media_meta = item.get("mediaMetadata", {})
                tags = media_meta.get("tags", [])
                is_hi_res = "HI_RES_LOSSLESS" in tags or item.get("audioQuality") == "HI_RES_LOSSLESS"
                is_lossless = is_hi_res or "LOSSLESS" in tags or item.get("audioQuality") == "LOSSLESS"

                cover_hash = item.get("album", {}).get("cover")
                artwork = ""
                if cover_hash:
                    artwork = f"https://resources.tidal.com/images/{cover_hash.replace('-', '/')}/1280x1280.jpg"

                tracks.append({
                    "id": f"td:{item['id']}",
                    "title": item.get("title", ""),
                    "artist": item.get("artist", {}).get("name", ""),
                    "album": item.get("album", {}).get("title", ""),
                    "duration": float(item.get("duration", 0)),
                    "artwork": artwork,
                    "artworkURL": artwork,
                    "format": "flac",
                    "audioQuality": "HI_RES_LOSSLESS" if is_hi_res else ("LOSSLESS" if is_lossless else "HIGH"),
                    "bitrate": 9216 if is_hi_res else (1411 if is_lossless else 320),
                    "audioModes": ["DOLBY_ATMOS"] if is_atmos else ["STEREO"],
                    "atmos": is_atmos,
                })
                if len(tracks) >= limit:
                    break
            return tracks
        except Exception as exc:
            logger.error(f"Tidal search failed for '{query}': {exc}")
            return []

    async def _fetch_playback_info(self, clean_id: str, qualities: list[str]) -> dict | None:
        """Fetch raw playback info from Tidal across quality tiers with country fallback.

        Loop order: quality → endpoint → country.
        Exhausts all countries for the primary endpoint (postpaywall) before trying the
        secondary endpoint. This keeps the fast-path (primary endpoint, home country, first
        quality) to a single request for most tracks.
        """
        countries = [self.country_code]
        for fallback_cc in ("ID", "GB", "US"):
            if fallback_cc not in countries:
                countries.append(fallback_cc)

        for q in qualities:
            for endpoint in ("playbackinfopostpaywall", "playbackinfo"):
                for cc in countries:
                    url = f"{API_BASE}/tracks/{clean_id}/{endpoint}"
                    params = {
                        "countryCode": cc,
                        "audioquality": q,
                        "playbackmode": "STREAM",
                        "assetpresentation": "FULL",
                    }
                    try:
                        resp = await self.client.get(url, headers=self._auth_headers(), params=params)
                        if resp.status_code == 401:
                            if await self._refresh_access_token():
                                resp = await self.client.get(url, headers=self._auth_headers(), params=params)

                        if resp.status_code == 429:
                            logger.warning(f"Tidal rate limit (429) on track {clean_id}. Retrying after 1.5s...")
                            await asyncio.sleep(1.5)
                            resp = await self.client.get(url, headers=self._auth_headers(), params=params)

                        if resp.status_code == 200:
                            return resp.json()
                    except Exception:
                        pass
        return None

    async def get_stream(self, track_id: str, quality: str = "lossless", public_host: str = "") -> dict | None:
        """Resolve track ID into signed FLAC CDN URL or DASH manifest with strict studio master upgrade."""
        if not self.is_configured():
            return None

        clean_id = track_id.replace("td:", "")
        original_id = clean_id

        data = await self._fetch_playback_info(clean_id, ["HI_RES_LOSSLESS", "LOSSLESS"])
        if not data:
            logger.info(f"Tidal playbackinfo returned no stream for track {clean_id}")
            return None

        # Tidal returned only HIGH (AAC): no FLAC available for this track.
        # Return None immediately so the caller's parallel Deezer pre-fetch delivers
        # without waiting. The old studio-master search loop ran 3-5s here and routinely
        # starved the Deezer fallback inside the 7s hard timeout.
        if data.get("audioQuality") == "HIGH":
            logger.info(f"Tidal track {clean_id} has no FLAC (HIGH only) — delegating to Deezer")
            return None

        try:
            raw_manifest = data.get("manifest")
            mime = data.get("manifestMimeType", "")
            raw_depth = data.get("bitDepth")
            raw_rate = data.get("sampleRate")
            bit_depth = raw_depth or 16
            sample_rate = raw_rate or 44100

            if "bts" in mime and raw_manifest:
                try:
                    decoded = json.loads(base64.b64decode(raw_manifest).decode("utf-8"))
                    direct_urls = decoded.get("urls", [])
                    if direct_urls:
                        url = direct_urls[0]
                        codecs_str = decoded.get("codecs", "").lower()
                        mime_str = decoded.get("mimeType", "").lower()
                        audio_q = data.get("audioQuality", "").upper()

                        # Lossy codecs that Tidal sometimes serves under LOSSLESS tier
                        # for rights-restricted tracks. Must be rejected explicitly.
                        LOSSY_CODECS = ("opus", "mp4a", "aac", "mp3", "he-aac")
                        has_lossy = any(c in codecs_str for c in LOSSY_CODECS)

                        is_actual_flac = (
                            not has_lossy
                            and (
                                "flac" in codecs_str
                                or "flac" in mime_str
                                # codecs field absent: trust audioQuality tier
                                or (not codecs_str and audio_q in ("LOSSLESS", "HI_RES_LOSSLESS"))
                            )
                        )
                        if not is_actual_flac:
                            logger.info(f"Tidal track {clean_id} is only available in lossy ({codecs_str}). Rejecting per FLAC-only rule.")
                            return None

                        is_mp4 = ".mp4" in url.lower() or "mp4" in mime_str
                        return {
                            "url": url,
                            "format": "flac",
                            "codec": "flac",
                            "container": "mp4" if is_mp4 else "flac",
                            "manifest": "none",
                            "encrypted": False,
                            "bitDepth": raw_depth or 16,
                            "sampleRate": raw_rate or 44100,
                            "bitrate": 1411,
                        }
                except Exception as decode_err:
                    logger.error(f"Failed to decode Tidal BTS manifest: {decode_err}")

            if "dash" in mime and raw_manifest:
                try:
                    decoded_xml = base64.b64decode(raw_manifest).decode("utf-8")
                    effective_host = (public_host or self.public_host).rstrip("/")

                    # Rewrite the initialization segment URL to our patched endpoint.
                    # Tidal's fLaC box in fMP4 DASH init segments encodes sampleRate=0,
                    # which crashes ExoPlayer's FragmentedMp4Extractor before audio starts.
                    init_match = re.search(r'initialization="([^"]+)"', decoded_xml)
                    if init_match:
                        orig_init_url = init_match.group(1)
                        self.init_url_cache[clean_id] = orig_init_url
                        if original_id != clean_id:
                            self.init_url_cache[original_id] = orig_init_url
                        patched_init_url = f"{effective_host}/dash/td/{original_id}/init.mp4" if effective_host else f"/dash/td/{original_id}/init.mp4"
                        decoded_xml = decoded_xml.replace(init_match.group(0), f'initialization="{patched_init_url}"')

                    self.dash_cache[clean_id] = decoded_xml
                    if original_id != clean_id:
                        self.dash_cache[original_id] = decoded_xml
                except Exception as e:
                    logger.error(f"Failed to decode DASH XML: {e}")
                    return None

                # Extract exact bandwidth from MPD Representation tag for accurate bitrate reporting
                bw_match = re.search(r'bandwidth="(\d+)"', decoded_xml)
                if bw_match:
                    calc_bitrate = max(int(bw_match.group(1)) // 1000, 1411)
                else:
                    calc_bitrate = int((sample_rate or 44100) * (bit_depth or 16) * 2 // 1000)

                effective_host = (public_host or self.public_host).rstrip("/")
                manifest_url = f"{effective_host}/dash/td/{original_id}.mpd" if effective_host else f"/dash/td/{original_id}.mpd"
                return {
                    "url": manifest_url,
                    "format": "flac",
                    "codec": "flac",
                    "container": "fmp4",
                    "manifest": "dash",
                    "encrypted": False,
                    "bitDepth": bit_depth,
                    "sampleRate": sample_rate,
                    "bitrate": calc_bitrate,
                }

            return None
        except Exception as exc:
            logger.error(f"Tidal stream resolution failed for {clean_id}: {exc}")
            return None
