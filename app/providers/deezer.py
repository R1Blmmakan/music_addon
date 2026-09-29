import re
from fastapi import HTTPException
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

# Deezer's checkForm CSRF token expires in ~1-2h; refresh every hour to prevent silent 401s.
_SESSION_TTL_SECONDS = 3600

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
        self._cdn_cache: dict[str, tuple[str, str, float]] = {}

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
                if not self._license_token:
                    logger.critical(
                        "Deezer session has no license_token — ARL may be expired. "
                        "Update DEEZER_ARL in your .env with a fresh cookie from browser."
                    )
                    return False
                self._session_initialized_at = time.monotonic()
                logger.info(f"Deezer authenticated as user ID {user_id}")
                return True

            logger.critical(
                "Deezer returned USER_ID=0 — ARL cookie is expired or invalid. "
                "Refresh DEEZER_ARL in your .env from browser DevTools → Application → Cookies → deezer.com."
            )
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
        """Search Deezer public catalogue with smart remix filtering and canonical ranking."""
        clean_query = re.sub(r'\s+(?:with|feat\.?|ft\.?|featuring)\s+', ' ', query.strip(), flags=re.IGNORECASE)
        clean_query = re.sub(r'\s+', ' ', clean_query).strip()
        if not clean_query:
            return []

        try:
            url = "https://api.deezer.com/search"
            # Fetch extra candidates to allow ranking and remix filtering
            fetch_limit = max(limit * 2, 25)
            resp = await self.client.get(url, params={"q": clean_query, "limit": fetch_limit})
            if resp.status_code != 200:
                return []

            items = resp.json().get("data", [])
            if not items:
                return []

            q_lower = clean_query.lower()
            version_keywords = ("remix", "mix", "dub", "edit", "version", "acoustic", "instrumental", "live", "feat", "ft")
            query_wants_version = any(re.search(r'\b' + re.escape(k) + r'\b', q_lower) for k in version_keywords)
            q_words = [w for w in re.sub(r'[^\w\s]', '', q_lower).split() if w not in ("with", "feat", "ft")]
            format_keywords = set(version_keywords)
            artist_q_words = [w for w in q_words if w not in format_keywords]

            junk_keywords = (
                "karaoke", "tribute", "originally performed", "in the style of",
                "backing track", "piano tribute", "lullaby", "8-bit", "emulation",
                "made popular by", "party tyme", "instrumental"
            )

            scored = []
            for idx, item in enumerate(items):
                raw_title = item.get("title", "").strip()
                title = raw_title.lower()
                title_version = (item.get("title_version") or "").strip().lower()
                core_title = re.sub(r'[\(\[](?:feat|ft|with)\.?[^\)\]]*[\)\]]', '', title, flags=re.IGNORECASE).strip()
                full_title = f"{title} {title_version}".strip()
                artist = item.get("artist", {}).get("name", "").strip().lower()
                rank = int(item.get("rank", 0) or 0)

                is_remix = any(k in full_title for k in ("remix", "mix", "dub", "edit", "re-mix"))
                has_version = bool(title_version) or is_remix

                # Demote junk / karaoke
                is_junk = any(k in full_title or k in artist for k in junk_keywords if k not in q_lower)
                score = 1000 if is_junk else 0

                # Version & Remix Gate:
                # If query specifically asks for a version/mix/remix/live/acoustic, reward the matching version!
                # If query does NOT ask for a version, demote remixes so the canonical studio track is preferred.
                if query_wants_version:
                    matched_v_words = any(w in full_title for w in version_keywords if w in q_lower)
                    if matched_v_words:
                        score -= 500  # Priority bonus for matching the requested version/mix
                    elif not has_version:
                        score += 300  # Demote plain track when user specifically asked for a version
                else:
                    if is_remix:
                        score += 800  # Heavily demote remixes when user did not ask for remix
                    elif not title_version:
                        score -= 200  # Bonus for canonical album track

                # Title matching (using core_title without feat fluff)
                if core_title == q_lower or full_title == q_lower:
                    score -= 800
                elif core_title in q_lower or q_lower.startswith(core_title):
                    score -= 600
                else:
                    matched_title = sum(1 for w in q_words if w in core_title.split() and w not in format_keywords)
                    score -= min(100 * matched_title, 350)
                    extra_words = sum(1 for w in core_title.split() if w not in q_words and w not in format_keywords)
                    score += 100 * extra_words

                # Artist matching (ignoring format keywords)
                matched_artist = sum(1 for w in artist_q_words if w in artist.split())
                score -= 100 * matched_artist
                if artist in q_lower or any(a in q_lower for a in artist.split()):
                    score -= 150

                # Synergy bonus
                if len(q_words) >= 2 and artist_q_words:
                    has_title_match = any(w in core_title.split() for w in q_words if w not in format_keywords)
                    has_artist_match = any(w in artist.split() for w in artist_q_words)
                    if has_title_match and has_artist_match:
                        score -= 300

                # Popularity rank bonus
                score -= min(int(rank / 10000), 100)
                scored.append((score, idx, item))

            scored.sort(key=lambda x: (x[0], x[1]))
            best_items = [x[2] for x in scored[:limit]]

            tracks = []
            for item in best_items:
                artwork = (
                    item.get("album", {}).get("cover_xl")
                    or item.get("album", {}).get("cover_big")
                    or item.get("album", {}).get("cover_medium")
                )
                raw_t = item.get("title", "").strip()
                t_ver = (item.get("title_version") or "").strip()
                dz_title = raw_t
                if t_ver and t_ver.lower() not in raw_t.lower():
                    dz_title = f"{raw_t} ({t_ver})".strip()

                tracks.append({
                    "id": f"dz:{item['id']}",
                    "title": dz_title,
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

        for attempt in range(2):
            url = f"https://www.deezer.com/ajax/gw-light.php?method=song.getData&api_version=1.0&api_token={self._api_token}"
            cookies = {"arl": self.arl}
            try:
                resp = await self.client.post(url, cookies=cookies, json={"sng_id": track_id})
                data = resp.json()

                # Deezer returns HTTP 200 even on auth errors; must inspect the body
                error = data.get("error")
                if error:
                    err_keys = list(error.keys()) if isinstance(error, dict) else [str(error)]
                    logger.warning(
                        f"Deezer song.getData error for {track_id} (attempt {attempt + 1}): {err_keys}"
                    )
                    if attempt == 0:
                        # Force re-auth: stale api_token is the most common cause
                        logger.info("Forcing Deezer session refresh after song.getData error")
                        self._api_token = None
                        self._license_token = None
                        self._session_initialized_at = 0.0
                        if not await self._init_session():
                            return None
                        continue  # retry with fresh token
                    return None

                results = data.get("results")
                if not results:
                    logger.warning(f"Deezer song.getData returned empty results for {track_id}")
                    return None
                return results

            except Exception as exc:
                logger.error(f"Failed to fetch song data for {track_id}: {exc}")
                return None

        return None

    async def get_cdn_url(self, track_id: str, quality: str = "lossless") -> tuple[str | None, str]:
        """Request media delivery stream URL from Deezer media API (cached for 2h)."""
        cache_key = f"{track_id}:{quality.lower()}"
        cached = self._cdn_cache.get(cache_key)
        if cached:
            url, fmt, ts = cached
            # Deezer CDN URLs are valid for 20h; 2h TTL (7200s) is completely safe
            if time.time() - ts < 7200:
                return url, fmt

        song_data = await self.get_track_data(track_id)
        if not song_data:
            return None, "flac"

        track_token = song_data.get("TRACK_TOKEN")
        if not track_token:
            logger.warning(f"Deezer track {track_id}: TRACK_TOKEN missing in song data (probable expired session or geo-block)")
            return None, "flac"
        if not self._license_token:
            logger.warning(f"Deezer track {track_id}: license_token is absent — session may not have fully initialised")
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
                logger.warning(
                    f"Deezer track {track_id}: media.deezer.com returned no media entries "
                    f"(errors: {data.get('errors', [])}). License token may be expired."
                )
                return None, "flac"

            first_media = media_list[0]
            sources = first_media.get("sources", [])
            chosen_format = first_media.get("format", "FLAC").lower()
            if sources:
                src_url = sources[0].get("url")
                if src_url:
                    if len(self._cdn_cache) > 200:
                        oldest_keys = sorted(self._cdn_cache, key=lambda k: self._cdn_cache[k][2])[:50]
                        for k in oldest_keys:
                            self._cdn_cache.pop(k, None)
                    self._cdn_cache[cache_key] = (src_url, chosen_format, time.time())
                return src_url, chosen_format
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
        """Fetch encrypted CDN stream, decrypt Blowfish chunks, and yield clean audio with byte-accurate Range support.

        No HEAD pre-flight: the CDN GET response itself carries Content-Length and
        Content-Range, so we let it drive sizing. This eliminates a blocking round-trip
        before the first audio byte and prevents the "stuck at upgrading quality" stall
        that occurs when HEAD returns no Content-Length and end_chunk is miscalculated.
        """
        cdn_url, audio_format = await self.get_cdn_url(track_id)
        if not cdn_url:
            raise RuntimeError(f"Unable to retrieve stream CDN URL for track {track_id}")

        key = get_track_blowfish_key(track_id)

        # Build CDN request headers from the client Range header (chunk-aligned).
        cdn_req_headers: dict[str, str] = {}
        client_start = 0

        if range_header:
            m = re.match(r"bytes=(\d+)-(\d*)", range_header.strip())
            if m:
                client_start = int(m.group(1))
                client_end_str = m.group(2)
                # Align start to Blowfish chunk boundary so decryption is correct.
                start_chunk = client_start // CHUNK_SIZE
                aligned_start = start_chunk * CHUNK_SIZE
                if client_end_str:
                    client_end = int(client_end_str)
                    end_chunk = client_end // CHUNK_SIZE
                    aligned_end = (end_chunk + 1) * CHUNK_SIZE - 1
                    cdn_req_headers["Range"] = f"bytes={aligned_start}-{aligned_end}"
                else:
                    cdn_req_headers["Range"] = f"bytes={aligned_start}-"
            else:
                start_chunk = 0
                aligned_start = 0
        else:
            start_chunk = 0
            aligned_start = 0

        # One network call: GET with optional Range. CDN responds with 200 or 206.
        cdn_resp = await self.client.get(cdn_url, headers=cdn_req_headers)

        cdn_status = cdn_resp.status_code
        cdn_cl = cdn_resp.headers.get("Content-Length")
        cdn_cr = cdn_resp.headers.get("Content-Range")  # e.g. "bytes 0-N/Total"

        # Derive total file size and byte window from CDN response headers.
        total_size = 0
        if cdn_cr:
            m_cr = re.match(r"bytes \d+-\d+/(\d+)", cdn_cr)
            if m_cr:
                total_size = int(m_cr.group(1))
        elif cdn_cl:
            total_size = int(cdn_cl)

        if range_header and cdn_status in (200, 206):
            m = re.match(r"bytes=(\d+)-(\d*)", range_header.strip())
            if m:
                start = int(m.group(1))
                raw_end = m.group(2)
                end = int(raw_end) if raw_end else (total_size - 1 if total_size else 0)
                if total_size and start >= total_size:
                    raise HTTPException(status_code=416, detail="Range Not Satisfiable")
                if total_size:
                    end = min(end, total_size - 1)
                status_code = 206
                content_length = end - start + 1
                content_range = f"bytes {start}-{end}/{total_size if total_size else '*'}"
            else:
                start = 0
                end = total_size - 1 if total_size else 0
                status_code = 200
                content_length = total_size or None
                content_range = None
        else:
            start = 0
            end = total_size - 1 if total_size else 0
            status_code = 200
            content_length = total_size or None
            content_range = None

        response_headers: dict[str, str] = {
            "Content-Type": "audio/flac" if audio_format == "flac" else "audio/mpeg",
            "Accept-Ranges": "bytes",
        }
        if content_length is not None:
            response_headers["Content-Length"] = str(content_length)
        else:
            response_headers["Transfer-Encoding"] = "chunked"
        if content_range is not None:
            response_headers["Content-Range"] = content_range

        prefix_to_skip = start - aligned_start

        async def audio_generator() -> AsyncGenerator[bytes, None]:
            chunk_idx = start_chunk
            buffer = bytearray()
            nonlocal prefix_to_skip
            bytes_remaining: float = (end - start + 1) if total_size else float("inf")

            async for raw_bytes in cdn_resp.aiter_bytes(chunk_size=CHUNK_SIZE):
                buffer.extend(raw_bytes)
                while len(buffer) >= CHUNK_SIZE and bytes_remaining > 0:
                    block = bytes(buffer[:CHUNK_SIZE])
                    del buffer[:CHUNK_SIZE]

                    if chunk_idx % 3 == 0:
                        decrypted = decrypt_stripe_chunk(block, key)
                    else:
                        decrypted = block

                    chunk_idx += 1

                    if prefix_to_skip > 0:
                        if prefix_to_skip >= len(decrypted):
                            prefix_to_skip -= len(decrypted)
                            continue
                        decrypted = decrypted[prefix_to_skip:]
                        prefix_to_skip = 0

                    to_yield = decrypted if bytes_remaining == float("inf") else decrypted[:int(bytes_remaining)]
                    bytes_remaining -= len(to_yield)
                    yield to_yield

            # Flush tail (final partial chunk smaller than CHUNK_SIZE)
            if buffer and bytes_remaining > 0:
                tail = bytes(buffer)
                if prefix_to_skip > 0:
                    tail = tail[prefix_to_skip:]
                yield tail if bytes_remaining == float("inf") else tail[:int(bytes_remaining)]

        return audio_generator(), response_headers, status_code
