"""
Tidal provider: OAuth token management, search scoring, and FLAC/Hi-Res stream resolution.
Handles BTS (progressive) and DASH manifests. Patches fMP4 init segments for ExoPlayer
because Tidal encodes sampleRate=0 in the fLaC box header for Hi-Res tracks.
"""
import asyncio
import base64
import html
import json
import logging
import re
import time
import unicodedata
from collections import OrderedDict
from pathlib import Path
import httpx
from app.providers.base import MusicProvider

logger = logging.getLogger("bitchord.tidal")

TOKEN_URL = "https://auth.tidal.com/v1/oauth2/token"
API_BASE = "https://api.tidal.com/v1"


def _strip_accents(text: str) -> str:
    """Normalize unicode characters to ASCII base form (e.g. 'São Paulo' -> 'Sao Paulo')."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


_VARIANT_KEYWORDS = (
    "remix", "mix", "dub", "edit", "re-mix", "solo", "acoustic",
    "instrumental", "live", "extended", "vip", "rework", "sped",
    "slowed", "karaoke", "cover", "tribute", "lullaby", "radio edit",
    "clean version",
)


def is_valid_tidal_match(
    cand_title: str,
    cand_artist: str,
    target_title: str,
    target_artist: str,
    cand_duration: int = 0,
    target_duration: int = 0,
) -> bool:
    """Strictly verify that a candidate track matches the target title, variant type, primary artist, and duration."""
    if not cand_title or not target_title:
        return False

    c_title_norm = _strip_accents(cand_title).lower().strip()
    t_title_norm = _strip_accents(target_title).lower().strip()

    # 1. Variant consistency: candidate must not introduce unrequested version keywords (e.g. 'Solo', 'Remix', 'Live')
    for kw in _VARIANT_KEYWORDS:
        pattern = r'\b' + re.escape(kw) + r'\b'
        if re.search(pattern, c_title_norm) and not re.search(pattern, t_title_norm):
            return False

    # 2. Core title equality (stripping collaborator credits and standard remaster/edition tags)
    def _core_norm(s: str) -> str:
        s = re.sub(r'[\(\[]\s*(?:feat|ft|with|featuring)\b[^\)\]]*[\)\]]', '', s, flags=re.IGNORECASE)
        s = re.sub(
            r'[\(\[][^\)\]]*(?:remaster(?:ed)?|deluxe|bonus|explicit|single version|album version|original)[^\)\]]*[\)\]]',
            '', s, flags=re.IGNORECASE,
        )
        s = re.sub(r'-\s*(?:remaster(?:ed)?|deluxe|single version|album version).*$', '', s, flags=re.IGNORECASE)
        return re.sub(r'[^a-z0-9]', '', s)

    c_core = _core_norm(c_title_norm)
    t_core = _core_norm(t_title_norm)
    if not c_core or not t_core or c_core != t_core:
        return False

    # 3. Primary artist verification: primary target artist must appear in candidate artists
    def _split_artists(artist_str: str) -> list[str]:
        norm = _strip_accents(artist_str).lower()
        parts = re.split(r'[,&;/]|\b(?:feat|ft|with|featuring)\b\.?', norm, flags=re.IGNORECASE)
        return [re.sub(r'[^a-z0-9]', '', p) for p in parts if re.sub(r'[^a-z0-9]', '', p)]

    t_artists = _split_artists(target_artist)
    c_artists = _split_artists(cand_artist)
    if t_artists and c_artists:
        primary_target = t_artists[0]
        if not any(
            primary_target == ca or (len(primary_target) >= 4 and (primary_target in ca or ca in primary_target))
            for ca in c_artists
        ):
            return False

    # 4. Duration sanity check (within 15 seconds when both durations are known)
    if cand_duration > 0 and target_duration > 0 and abs(cand_duration - target_duration) > 15:
        return False

    return True

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


def score_tidal_candidate(item: dict, clean_query: str) -> float | None:
    """Score a Tidal candidate item against clean_query. Returns None if candidate is irrelevant/junk."""
    if item.get("type") == "video":
        return None

    raw_title = item.get("title", "").strip()
    t_title = _strip_accents(raw_title).lower()
    t_version = _strip_accents(item.get("version") or "").strip().lower()
    core_title = re.sub(r'[\(\[](?:feat|ft|with|featuring)\.?[^\)\]]*[\)\]]', '', t_title, flags=re.IGNORECASE).strip()
    full_title = f"{t_title} {t_version}".strip()
    popularity = int(item.get("popularity", 0) or 0)

    all_artists = [item.get("artist", {}).get("name", "")]
    for a in item.get("artists", []):
        a_name = a.get("name")
        if a_name and a_name not in all_artists:
            all_artists.append(a_name)
    t_artists_str = _strip_accents(" ".join(all_artists)).strip().lower()
    t_album = _strip_accents(item.get("album", {}).get("title", "")).strip().lower()

    query_lower = _strip_accents(clean_query).lower()
    version_keywords = (
        "remix", "mix", "dub", "edit", "version", "solo", "acoustic",
        "instrumental", "live", "extended", "vip", "rework", "sped",
        "slowed", "feat", "ft",
    )
    query_wants_version = any(re.search(r'\b' + re.escape(k) + r'\b', query_lower) for k in version_keywords)
    query_words = [w for w in re.sub(r'[^\w\s]', '', query_lower).split() if w not in ("with", "feat", "ft")]
    format_keywords = set(version_keywords)
    content_query_words = [w for w in query_words if w not in format_keywords]
    artist_query_words = content_query_words

    title_word_set = set(re.sub(r'[^\w\s]', ' ', core_title).split())
    artist_word_set = set(re.sub(r'[^\w\s]', ' ', t_artists_str).split())

    # Gate 1: Candidate MUST match at least one non-format content word in title or artist,
    # and if the query specifies both title and artist words, it must match both.
    if content_query_words:
        matched_title_cnt = sum(1 for w in content_query_words if w in title_word_set)
        matched_artist_cnt = sum(1 for w in content_query_words if w in artist_word_set)
        if matched_title_cnt == 0 and matched_artist_cnt == 0:
            return None

        unmatched_by_title = [w for w in content_query_words if w not in title_word_set]
        unmatched_by_artist = [w for w in content_query_words if w not in artist_word_set]
        # Reject wrong-artist covers when query contains artist words beyond the title
        if unmatched_by_title and matched_artist_cnt == 0:
            return None
        # Reject unrelated songs by the same artist when query contains title words beyond the artist
        if unmatched_by_artist and matched_title_cnt == 0:
            return None

    # Gate 2: Junk, parody, and tribute filtering
    junk_keywords = (
        "karaoke", "tribute", "originally performed",
        "in the style of", "backing track", "piano tribute",
        "lullaby", "instrumental", "cover", "speed up",
        "sped up", "slowed", "acoustic tribute", "made famous by",
        "party tyme", "sound-alike", "soundalike", "tribute band"
    )
    if any(k in full_title or k in t_artists_str or k in t_album for k in junk_keywords if k not in query_lower):
        return None

    # Gate 3: Compilation and parody bot checks
    is_compilation = any(k in t_album for k in (
        "summer hits", "hot hits", "top 50", "top 100", "various artists",
        "best of 20", "party hits", "gym hits", "greatest hits 20"
    ))
    if is_compilation and len(content_query_words) >= 2:
        matched_artist = sum(1 for w in content_query_words if w in artist_word_set)
        matched_title = sum(1 for w in content_query_words if w in title_word_set)
        if matched_title > 0 and matched_artist == 0:
            return None
        if matched_artist < 2 and any(w in content_query_words for w in ("clean", "dua", "blackpink", "orange", "lauv")):
            return None

    score = 0.0

    # Version & Variant Gate (including 'solo', 'acoustic', 'live', 'extended', 'remix'):
    variant_tokens = (
        "remix", "mix", "dub", "edit", "re-mix", "solo", "acoustic",
        "instrumental", "live", "extended", "vip", "rework", "sped", "slowed",
    )
    is_variant = any(re.search(r'\b' + re.escape(k) + r'\b', full_title) for k in variant_tokens)
    has_version = bool(t_version) or is_variant
    if query_wants_version:
        matched_v_words = any(w in full_title for w in version_keywords if w in query_lower)
        if matched_v_words:
            score -= 500.0  # Priority bonus for matching requested version/mix
        elif not has_version:
            score += 300.0  # Demote plain track when user specifically asked for a version
    else:
        if is_variant:
            score += 800.0  # Heavily demote variants (e.g. Solo, Remix, Live) when user did not ask for one
        elif t_version and not any(k in t_version for k in ("explicit", "remaster", "album version", "single version", "original")):
            score += 400.0
        else:
            score -= 200.0  # Bonus for canonical album track

    # Title matching
    if core_title == query_lower or full_title == query_lower:
        score -= 800.0
    elif core_title in query_lower or query_lower.startswith(core_title):
        score -= 600.0
    else:
        matched_title_words = sum(1 for w in query_words if w in title_word_set and w not in format_keywords)
        score -= min(100.0 * matched_title_words, 350.0)
        extra_words = sum(1 for w in title_word_set if w not in query_words and w not in format_keywords)
        score += 100.0 * extra_words

    # Artist matching
    matched_artist_words = sum(1 for w in artist_query_words if w in artist_word_set)
    if matched_artist_words > 0:
        score -= 100.0 * matched_artist_words
        if t_artists_str in query_lower or any(_strip_accents(a).lower() in query_lower for a in all_artists if a):
            score -= 200.0

    # Penalize query words missing from both title and artist
    unmatched_anywhere = sum(1 for w in content_query_words if w not in title_word_set and w not in artist_word_set)
    if unmatched_anywhere > 0:
        score += 250.0 * unmatched_anywhere

    # Synergy bonus when query words hit both title and artist
    if len(query_words) >= 2 and artist_query_words:
        has_title_match = any(w in title_word_set for w in query_words if w not in format_keywords)
        has_artist_match = any(w in artist_word_set for w in artist_query_words)
        if has_title_match and has_artist_match:
            score -= 300.0

    audio_modes = item.get("audioModes", [])
    tags = item.get("mediaMetadata", {}).get("tags", [])
    is_hi_res = (
        "HIRES_LOSSLESS" in tags
        or "HI_RES_LOSSLESS" in tags
        or item.get("audioQuality") in ("HI_RES_LOSSLESS", "HIRES_LOSSLESS")
    )
    is_lossless = is_hi_res or "LOSSLESS" in tags or item.get("audioQuality") == "LOSSLESS"
    if "DOLBY_ATMOS" in audio_modes and not is_lossless:
        score += 600.0
    elif is_hi_res:
        score -= 250.0
    elif is_lossless:
        score -= 50.0
    else:
        score += 400.0

    # Explicit vs Clean version weighting:
    is_explicit = bool(item.get("explicit", False))
    clean_in_query = bool(re.search(r'\bclean\b', query_lower))
    is_clean_version = any(k in full_title for k in ("clean", "radio edit", "clean version"))
    if not clean_in_query:
        # Default: user prefers the unedited explicit original track
        if is_explicit:
            score -= 50.0
        if is_clean_version:
            score += 200.0  # Penalize censored/radio edits
    else:
        # Explicit query for clean version
        if is_clean_version:
            score -= 200.0
        if is_explicit:
            score += 300.0

    if "soundtrack" in t_album or "compilation" in t_album or is_compilation:
        score += 350.0

    score -= min(popularity, 100)
    return score


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
        # Capped at 256-512 entries each to prevent unbounded RAM growth on long-running containers
        self.dash_cache: _LRUCache = _LRUCache(maxsize=256)
        self.init_url_cache: _LRUCache = _LRUCache(maxsize=256)
        self.init_segment_cache: _LRUCache = _LRUCache(maxsize=256)
        self.no_flac_cache: _LRUCache = _LRUCache(maxsize=512)
        self.track_meta_cache: _LRUCache = _LRUCache(maxsize=512)
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

            token_cid = self.token_data.get("client_id", "")
            token_csec = self.token_data.get("client_secret", "")
            client_id = token_cid or self.client_id
            if token_cid and token_cid != self.client_id:
                client_secret = token_csec
            else:
                client_secret = token_csec or self.client_secret
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

            scored_items = []
            for idx, item in enumerate(items):
                score = score_tidal_candidate(item, clean_query)
                if score is not None:
                    scored_items.append((score, idx, item))

            scored_items.sort(key=lambda x: (x[0], x[1]))
            best_items = [x[2] for x in scored_items]

            tracks = []
            for item in best_items:
                audio_modes = item.get("audioModes", [])
                is_atmos = "DOLBY_ATMOS" in audio_modes
                media_meta = item.get("mediaMetadata", {})
                tags = media_meta.get("tags", [])
                is_hi_res = (
                    "HIRES_LOSSLESS" in tags
                    or "HI_RES_LOSSLESS" in tags
                    or item.get("audioQuality") in ("HI_RES_LOSSLESS", "HIRES_LOSSLESS")
                )
                is_lossless = is_hi_res or "LOSSLESS" in tags or item.get("audioQuality") == "LOSSLESS"

                album_obj = item.get("album", {}) or {}
                cover_hash = album_obj.get("cover")
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
                is_explicit = bool(item.get("explicit", False))
                duration_int = int(round(float(item.get("duration", 0) or 0)))
                self.track_meta_cache[str(item["id"])] = {
                    "title": display_title,
                    "artist": display_artist,
                    "duration": duration_int,
                    "album_id": album_obj.get("id"),
                }

                tracks.append({
                    "id": f"td:{item['id']}",
                    "title": display_title,
                    "artist": display_artist,
                    "album": album_obj.get("title", ""),
                    "album_id": album_obj.get("id"),
                    "duration": duration_int,
                    "artwork": artwork,
                    "artworkURL": artwork,
                    "format": "flac" if is_lossless else "aac",
                    "audioQuality": "HI_RES_LOSSLESS" if is_hi_res else ("LOSSLESS" if is_lossless else "HIGH"),
                    "bitrate": 9216 if is_hi_res else (1411 if is_lossless else 320),
                    "audioModes": ["DOLBY_ATMOS"] if is_atmos else ["STEREO"],
                    "atmos": is_atmos,
                    "isrc": item.get("isrc") or None,
                    "explicit": is_explicit,
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
        Once any country/endpoint returns HTTP 200 for a given quality tier `q`:
          - If `returned_q` is lossless (`HI_RES_LOSSLESS` or `LOSSLESS`), return immediately.
          - If `returned_q` is downgraded (`HIGH`/`LOW`), break out of BOTH the country and
            endpoint loops for `q` and immediately test the next quality tier (cutting a
            non-FLAC check from 8 HTTP calls down to 2).
        """
        countries = [self.country_code]
        for fallback_cc in ("ID", "GB", "US"):
            if fallback_cc not in countries:
                countries.append(fallback_cc)

        refresh_attempted = False
        lossy_candidate = None

        for q in qualities:
            got_response_for_q = False
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
                            data = resp.json()
                            returned_q = (data.get("audioQuality") or "").upper()
                            if q in ("HI_RES_LOSSLESS", "LOSSLESS"):
                                if returned_q in ("HI_RES_LOSSLESS", "LOSSLESS"):
                                    return data
                                if not lossy_candidate:
                                    lossy_candidate = data
                                got_response_for_q = True
                                break
                            else:
                                return data
                    except Exception as e:
                        logger.debug(f"Exception fetching {url}: {e}")
                if got_response_for_q:
                    break

        return lossy_candidate

    async def _find_validated_studio_master(self, clean_id: str) -> tuple[str, dict] | None:
        """Search Tidal for an alternate Studio Master (HI_RES_LOSSLESS / LOSSLESS) edition
        of the exact same song and artist when `clean_id` only serves HIGH/LOW."""
        meta = self.track_meta_cache.get(clean_id)
        if not meta:
            try:
                resp = await self.client.get(
                    f"{API_BASE}/tracks/{clean_id}",
                    headers=self._auth_headers(),
                    params={"countryCode": self.country_code},
                )
                if resp.status_code == 200:
                    raw = resp.json()
                    t_title = (raw.get("title") or "").strip()
                    t_ver = (raw.get("version") or "").strip()
                    full_t = f"{t_title} ({t_ver})" if t_ver and t_ver.lower() not in t_title.lower() else t_title
                    artists_list = [raw.get("artist", {}).get("name", "")]
                    for a in raw.get("artists", []):
                        a_name = a.get("name")
                        if a_name and a_name not in artists_list:
                            artists_list.append(a_name)
                    meta = {
                        "title": full_t,
                        "artist": ", ".join(artists_list).strip(),
                        "duration": int(round(float(raw.get("duration", 0) or 0))),
                    }
                    self.track_meta_cache[clean_id] = meta
            except Exception as e:
                logger.debug(f"Could not fetch track metadata for studio master upgrade on {clean_id}: {e}")

        if not meta or not meta.get("title") or not meta.get("artist"):
            return None

        orig_title = meta["title"]
        orig_artist = meta["artist"]
        orig_duration = meta.get("duration", 0)
        primary_artist = re.split(r'[,&;/]|\b(?:feat|ft|with|featuring)\b\.?', orig_artist, flags=re.IGNORECASE)[0].strip()

        candidates = await self.search(f"{orig_title} {primary_artist}", limit=10)
        # Prioritize HI_RES_LOSSLESS candidates first (essential for TV/device tokens where
        # 16-bit LOSSLESS BTS is downgraded to HIGH while HI_RES_LOSSLESS DASH succeeds).
        candidates.sort(key=lambda c: 0 if c.get("audioQuality") == "HI_RES_LOSSLESS" else 1)

        tried = 0
        now = time.time()
        hires_album_ids: list[int] = []
        norm_primary_artist = _strip_accents(primary_artist).lower()

        for cand in candidates:
            cand_id = str(cand.get("id", "")).replace("td:", "")
            if not cand_id or cand_id == clean_id:
                continue
            cand_artist_norm = _strip_accents(cand.get("artist", "")).lower()
            cand_album_id = cand.get("album_id") or (self.track_meta_cache.get(cand_id) or {}).get("album_id")
            if (
                cand.get("audioQuality") == "HI_RES_LOSSLESS"
                and cand_album_id
                and norm_primary_artist
                and norm_primary_artist in cand_artist_norm
                and cand_album_id not in hires_album_ids
            ):
                hires_album_ids.append(cand_album_id)

            cached_fail_ts = self.no_flac_cache.get(cand_id)
            if cached_fail_ts and (now - cached_fail_ts < 600.0):
                continue
            if cand.get("audioQuality") not in ("HI_RES_LOSSLESS", "LOSSLESS"):
                continue
            if not is_valid_tidal_match(
                cand.get("title", ""),
                cand.get("artist", ""),
                orig_title,
                orig_artist,
                cand.get("duration", 0),
                orig_duration,
            ):
                continue

            tried += 1
            cand_data = await self._fetch_playback_info(cand_id, ["HI_RES_LOSSLESS", "LOSSLESS"])
            if cand_data:
                cand_q = (cand_data.get("audioQuality") or "").upper()
                if cand_q in ("HI_RES_LOSSLESS", "LOSSLESS"):
                    logger.info(
                        f"Tidal track {clean_id} upgraded to validated studio master {cand_id} ({cand_q})"
                    )
                    return cand_id, cand_data
                self.no_flac_cache[cand_id] = time.time()
            if tried >= 2:
                break

        # Tidal's /v1/search/tracks collapses tracks sharing the same ISRC, which can hide
        # a newer HI_RES_LOSSLESS album track behind an older 16-bit single (e.g. "Close To Me"
        # single 96905760 hides HI_RES_LOSSLESS track 568446925 on album 568446924 "EG.0",
        # even though "Close To Me (Solo)" 568446931 from album 568446924 is in search results).
        for album_id in hires_album_ids[:2]:
            try:
                resp = await self.client.get(
                    f"{API_BASE}/albums/{album_id}/tracks",
                    headers=self._auth_headers(),
                    params={"countryCode": self.country_code, "limit": 50},
                )
                if resp.status_code != 200:
                    continue
                for item in resp.json().get("items", []):
                    alb_cand_id = str(item.get("id", ""))
                    if not alb_cand_id or alb_cand_id == clean_id:
                        continue
                    tags = item.get("mediaMetadata", {}).get("tags", [])
                    if "HIRES_LOSSLESS" not in tags and "HI_RES_LOSSLESS" not in tags:
                        continue
                    raw_t = (item.get("title") or "").strip()
                    t_ver = (item.get("version") or "").strip()
                    full_t = f"{raw_t} ({t_ver})" if t_ver and t_ver.lower() not in raw_t.lower() else raw_t
                    a_list = [item.get("artist", {}).get("name", "")]
                    for a in item.get("artists", []):
                        a_name = a.get("name")
                        if a_name and a_name not in a_list:
                            a_list.append(a_name)
                    full_a = ", ".join(a_list).strip()
                    dur = int(round(float(item.get("duration", 0) or 0)))
                    if not is_valid_tidal_match(full_t, full_a, orig_title, orig_artist, dur, orig_duration):
                        continue
                    cand_data = await self._fetch_playback_info(alb_cand_id, ["HI_RES_LOSSLESS", "LOSSLESS"])
                    if cand_data:
                        cand_q = (cand_data.get("audioQuality") or "").upper()
                        if cand_q in ("HI_RES_LOSSLESS", "LOSSLESS"):
                            logger.info(
                                f"Tidal track {clean_id} upgraded via Hi-Res album {album_id} to studio master {alb_cand_id} ({cand_q})"
                            )
                            return alb_cand_id, cand_data
                        self.no_flac_cache[alb_cand_id] = time.time()
            except Exception as e:
                logger.debug(f"Could not inspect Hi-Res album {album_id} for {clean_id}: {e}")

        return None

    async def get_stream(
        self,
        track_id: str,
        quality: str = "lossless",
        public_host: str = "",
        _allow_upgrade: bool = True,
    ) -> dict | None:
        """Resolve track ID into signed FLAC/AAC CDN URL or DASH manifest."""
        if not self.is_configured():
            return None

        clean_id = track_id.replace("td:", "")
        original_id = clean_id
        q_norm = quality.lower().strip()

        if q_norm == "high":
            qualities_to_try = ["HIGH"]
        elif q_norm == "low":
            qualities_to_try = ["LOW"]
        else:
            q_norm = "lossless"
            qualities_to_try = ["HI_RES_LOSSLESS", "LOSSLESS"]

        now = time.time()
        cached_fail_ts = self.no_flac_cache.get(clean_id) if q_norm == "lossless" else None
        if cached_fail_ts and (now - cached_fail_ts < 600.0):
            data = None
            ret_audio_q = "HIGH"
        else:
            data = await self._fetch_playback_info(clean_id, qualities_to_try)
            if not data:
                logger.info(f"Tidal playbackinfo returned no stream for track {clean_id}")
                return None
            ret_audio_q = (data.get("audioQuality") or "").upper()

        # If caller requested lossless and this track ID only returned lossy (HIGH/LOW),
        # cache the negative result and check if a validated Studio Master edition exists on Tidal.
        if q_norm == "lossless" and ret_audio_q not in ("HI_RES_LOSSLESS", "LOSSLESS"):
            self.no_flac_cache[clean_id] = time.time()
            if _allow_upgrade:
                upgraded = await self._find_validated_studio_master(clean_id)
                if upgraded:
                    clean_id, data = upgraded
                    ret_audio_q = (data.get("audioQuality") or "").upper()
                else:
                    logger.info(f"Tidal track {clean_id} has no FLAC ({ret_audio_q} only) — delegating to fallback")
                    return None
            else:
                logger.info(f"Tidal track {clean_id} has no FLAC ({ret_audio_q} only)")
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

                        if q_norm == "lossless":
                            if not is_actual_flac:
                                logger.info(f"Tidal track {clean_id} is only available in lossy ({codecs_str}). Rejecting per FLAC-only rule.")
                                return None

                            is_mp4 = ".mp4" in url.lower() or "mp4" in mime_str
                            is_hi_res = (bit_depth > 16) or (audio_q == "HI_RES_LOSSLESS")
                            return {
                                "url": url,
                                "format": "flac",
                                "codec": "flac",
                                "container": "mp4" if is_mp4 else "flac",
                                "manifest": "none",
                                "encrypted": False,
                                "bitDepth": raw_depth or 16,
                                "sampleRate": raw_rate or 44100,
                                "bitrate": 9216 if is_hi_res else 1411,
                                "audioQuality": "HI_RES_LOSSLESS" if is_hi_res else "LOSSLESS",
                            }
                        else:
                            # High or Low lossy tier requested
                            is_mp4 = ".mp4" in url.lower() or "mp4" in mime_str
                            target_bitrate = 320 if q_norm == "high" else 128
                            return {
                                "url": url,
                                "format": "flac" if is_actual_flac else "aac",
                                "codec": "flac" if is_actual_flac else "aac",
                                "container": "mp4" if is_mp4 else ("flac" if is_actual_flac else "m4a"),
                                "manifest": "none",
                                "encrypted": False,
                                "bitDepth": raw_depth or 16,
                                "sampleRate": raw_rate or 44100,
                                "bitrate": 1411 if is_actual_flac else target_bitrate,
                                "audioQuality": "HIGH" if q_norm == "high" else "LOW",
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
                        orig_init_url = html.unescape(init_match.group(1))
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

                is_flac_dash = "flac" in decoded_xml.lower()
                bw_match = re.search(r'bandwidth="(\d+)"', decoded_xml)
                if bw_match:
                    calc_bitrate = max(int(bw_match.group(1)) // 1000, 128)
                else:
                    if q_norm == "lossless":
                        calc_bitrate = int((sample_rate or 44100) * (bit_depth or 16) * 2 // 1000)
                    else:
                        calc_bitrate = 320 if q_norm == "high" else 128

                manifest_url = f"{effective_host}/dash/td/{original_id}.mpd" if effective_host else f"/dash/td/{original_id}.mpd"
                is_hi_res = (bit_depth > 16) or (ret_audio_q == "HI_RES_LOSSLESS")
                resolved_aq = (
                    "HI_RES_LOSSLESS" if (q_norm == "lossless" and is_hi_res)
                    else ("LOSSLESS" if q_norm == "lossless" else ("HIGH" if q_norm == "high" else "LOW"))
                )

                return {
                    "url": manifest_url,
                    "format": "flac" if is_flac_dash else "aac",
                    "codec": "flac" if is_flac_dash else "aac",
                    "container": "fmp4",
                    "manifest": "dash",
                    "encrypted": False,
                    "bitDepth": bit_depth,
                    "sampleRate": sample_rate,
                    "bitrate": calc_bitrate,
                    "audioQuality": resolved_aq,
                }

            return None
        except Exception as exc:
            logger.error(f"Tidal stream resolution failed for {clean_id}: {exc}")
            return None
