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
import time
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
    def __init__(self, token_file: str = "token.json", country_code: str = "ID", public_host: str = "", client_id: str = "", client_secret: str = ""):
        self.token_file = Path(token_file)
        self.configured_country_code = country_code or "ID"
        self.country_code = self.configured_country_code
        self.public_host = public_host.rstrip("/") if public_host else ""
        self.client_id = client_id
        self.client_secret = client_secret
        self.client = httpx.AsyncClient(timeout=15.0)
        self.token_data: dict | None = None
        self._refresh_lock = asyncio.Lock()
        self._last_refresh_time: float = 0.0
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
        if self.token_file.is_dir():
            logger.error(f"Tidal token path {self.token_file} is a directory! A regular file was expected.")
            self.token_data = None
            return

        target_file = self.token_file
        if not target_file.exists():
            # Fallback to root token.json if data/token.json does not exist yet
            alt = target_file.parent.parent / "token.json"
            if alt.is_file():
                target_file = alt

        if target_file.is_file():
            try:
                with open(target_file, "r", encoding="utf-8") as f:
                    self.token_data = json.load(f)
                self.token_file = target_file
                self._last_refresh_time = time.time()
                logger.info(
                    f"Loaded Tidal token file {self.token_file} "
                    f"(client_id: {self.token_data.get('client_id')!r}, "
                    f"user_id: {self.token_data.get('user_id')!r})"
                )
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
        """Refresh expired access token using stored refresh token with concurrency protection."""
        if not self.token_data or not self.token_data.get("refresh_token"):
            return False

        # Fast path: if another coroutine just refreshed within the last 10 seconds, reuse it
        now = time.time()
        if now - self._last_refresh_time < 10.0:
            return True

        async with self._refresh_lock:
            # Re-check under lock in case another request completed refresh while we waited
            now = time.time()
            if now - self._last_refresh_time < 10.0:
                return True

            client_id = self.client_id or self.token_data.get("client_id", "")
            client_secret = self.client_secret or self.token_data.get("client_secret", "")
            if not client_id:
                logger.error("Tidal token refresh failed: client_id not configured. Set TIDAL_CLIENT_ID in .env.")
                return False
            refresh_token = self.token_data["refresh_token"]

            data = {
                "client_id": client_id,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
                "scope": "r_usr+w_usr+w_sub",
            }
            if client_secret:
                data["client_secret"] = client_secret

            headers = {
                "User-Agent": "okhttp/5.3.2",
                "Accept": "application/json",
                "X-Platform": "android",
                "X-Tidal-Platform": "android",
            }

            logger.info(f"Attempting Tidal token refresh: client_id={client_id!r}, has_secret={bool(client_secret)}")
            try:
                resp = await self.client.post(TOKEN_URL, data=data, headers=headers)
                if resp.status_code == 200:
                    new_info = resp.json()
                    self.token_data["access_token"] = new_info["access_token"]
                    if "refresh_token" in new_info:
                        self.token_data["refresh_token"] = new_info["refresh_token"]

                    with open(self.token_file, "w", encoding="utf-8") as f:
                        json.dump(self.token_data, f, indent=2)

                    self._last_refresh_time = time.time()
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
        headers = {
            "User-Agent": "okhttp/5.3.2",
            "Accept": "application/json",
            "X-Platform": "android",
            "X-Tidal-Platform": "android",
        }
        if access_token and not access_token.startswith("CzET"):
            headers["authorization"] = f"Bearer {access_token}"
        else:
            headers["x-tidal-token"] = access_token or "CzET4vdadNUFQ5JU"
        return headers

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
                        if time.time() - self._last_refresh_time > 60.0:
                            if await self._refresh_access_token():
                                resp = await self.client.get(url, headers=self._auth_headers(), params=params)
                            else:
                                logger.error("Tidal token refresh failed during search.")
                                return []

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
            query_words = [w for w in re.sub(r'[^\w\s]', '', query_lower).split() if w not in ("with", "feat", "ft")]
            query_wants_remix = "remix" in query_lower

            format_keywords = {"remix", "mix", "edit", "version", "dub", "live", "acoustic", "instrumental"}
            artist_query_words = [w for w in query_words if w not in format_keywords]

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

                raw_title = item.get("title", "").strip()
                t_title = raw_title.lower()
                t_version = (item.get("version") or "").strip()
                core_title = re.sub(r'[\(\[](?:feat|ft|with)\.?[^\)\]]*[\)\]]', '', t_title, flags=re.IGNORECASE).strip()
                full_title = f"{t_title} {t_version.lower()}".strip()
                t_artist = item.get("artist", {}).get("name", "").strip().lower()
                popularity = int(item.get("popularity", 0) or 0)

                all_artists = [item.get("artist", {}).get("name", "")]
                for a in item.get("artists", []):
                    a_name = a.get("name")
                    if a_name and a_name not in all_artists:
                        all_artists.append(a_name)
                t_artists_str = " ".join(all_artists).strip().lower()

                is_remix = any(k in full_title for k in ("remix", "mix", "dub", "edit", "re-mix"))

                # Demote junk unless the query itself requests it
                is_junk = any(
                    k in full_title or k in t_artists_str
                    for k in junk_keywords
                    if k not in query_lower
                )
                score = 1000 if is_junk else 0

                # Strict remix permission gate
                if query_wants_remix:
                    if is_remix:
                        score -= 500  # Priority bonus for remix
                    else:
                        score += 400  # Demote original when user specifically asked for remix
                else:
                    if is_remix:
                        score += 800  # Heavily demote remixes when user did not ask for remix
                    elif not t_version:
                        score -= 200  # Bonus for canonical album track

                # Title matching (using core_title without feat fluff)
                if core_title == query_lower or full_title == query_lower:
                    score -= 800
                elif core_title in query_lower or query_lower.startswith(core_title):
                    score -= 600
                else:
                    matched_title_words = sum(1 for w in query_words if w in core_title.split() and w not in format_keywords)
                    score -= min(100 * matched_title_words, 350)
                    extra_words = sum(1 for w in core_title.split() if w not in query_words and w not in format_keywords)
                    score += 100 * extra_words

                # Artist matching (ignoring format keywords so 'Remix Guys' don't get artist points)
                matched_artist_words = sum(1 for w in artist_query_words if any(w == a_w for a_w in t_artists_str.split()))
                if matched_artist_words > 0:
                    score -= 100 * matched_artist_words
                    if t_artists_str in query_lower or any(a.lower() in query_lower for a in all_artists):
                        score -= 200

                # Synergy bonus when query words hit both title and artist
                if len(query_words) >= 2 and artist_query_words:
                    has_title_match = any(w in core_title.split() for w in query_words if w not in format_keywords)
                    has_artist_match = any(w in t_artists_str.split() for w in artist_query_words)
                    if has_title_match and has_artist_match:
                        score -= 300

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

                raw_t = item.get("title", "").strip()
                t_ver = (item.get("version") or "").strip()
                display_title = f"{raw_t} ({t_ver})" if t_ver else raw_t

                artists_list = [item.get("artist", {}).get("name", "")]
                for a in item.get("artists", []):
                    a_name = a.get("name")
                    if a_name and a_name not in artists_list:
                        artists_list.append(a_name)
                display_artist = ", ".join(artists_list).strip()

                tracks.append({
                    "id": f"td:{item['id']}",
                    "title": display_title,
                    "artist": display_artist,
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

        refresh_attempted = False
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
                            logger.info(f"Tidal 401 on {endpoint} ({cc}, {q}): {resp.text[:120]}")
                            # subStatus 4005 is "Asset is not ready for playback" (e.g. tier/region restriction),
                            # NOT an expired OAuth access token. Do NOT trigger a token refresh on 4005.
                            is_asset_unready = False
                            try:
                                err_json = resp.json()
                                if err_json.get("subStatus") == 4005:
                                    is_asset_unready = True
                            except Exception:
                                pass

                            if not is_asset_unready and not refresh_attempted and (time.time() - self._last_refresh_time > 60.0):
                                refresh_attempted = True
                                if await self._refresh_access_token():
                                    resp = await self.client.get(url, headers=self._auth_headers(), params=params)

                        if resp.status_code == 429:
                            logger.warning(f"Tidal rate limit (429) on track {clean_id}. Retrying after 1.5s...")
                            await asyncio.sleep(1.5)
                            resp = await self.client.get(url, headers=self._auth_headers(), params=params)

                        if resp.status_code == 200:
                            return resp.json()
                    except Exception as e:
                        logger.debug(f"Exception fetching {url}: {e}")
        return None

    async def get_stream(self, track_id: str, quality: str = "lossless", public_host: str = "") -> dict | None:
        """Resolve track ID into signed FLAC CDN URL or DASH manifest with strict studio master upgrade."""
        if not self.is_configured():
            return None

        clean_id = track_id.replace("td:", "")
        original_id = clean_id

        # Prioritize HI_RES_LOSSLESS (24-bit Studio Master / Tidal Max) first,
        # gracefully falling back to LOSSLESS (16-bit CD Quality FLAC)
        qualities_to_try = ["HI_RES_LOSSLESS", "LOSSLESS"]
        data = await self._fetch_playback_info(clean_id, qualities_to_try)
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
