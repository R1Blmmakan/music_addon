import re
import base64
import asyncio
import hashlib
import hmac as _hmac
import logging
import time
from urllib.parse import urlparse
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.providers.deezer import DeezerProvider
from app.providers.tidal import TidalProvider
from app.schemas import ManifestResponse, SearchResponse, StreamResponse, TrackItem, ResolveIsrcResponse, ResolveResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("bitchord.unified")

deezer = DeezerProvider(settings.deezer_arl, settings.public_host)
tidal = TidalProvider(
    settings.tidal_token_file,
    settings.tidal_country_code,
    settings.public_host,
    client_id=settings.tidal_client_id,
    client_secret=settings.tidal_client_secret,
)

_AUDIO_TTL = 3600  # 1 hour: enough to cover any single playback session

def _sign_path(path: str) -> str:
    """Append exp + HMAC-SHA256 signature to a proxy path. No-op in open mode."""
    secret = settings.audio_signing_secret or settings.access_token
    if not secret:
        return path
    exp = int(time.time()) + _AUDIO_TTL
    tag = _hmac.new(secret.encode(), f"{path}:{exp}".encode(), hashlib.sha256).hexdigest()
    return f"{path}?exp={exp}&sig={tag}"

def _verify_path_sig(path: str, exp: str | None, sig: str | None) -> bool:
    """Verify HMAC-SHA256 on an internal proxy URL. Returns True in open mode."""
    secret = settings.audio_signing_secret or settings.access_token
    if not secret:
        return True
    if not exp or not sig:
        return False
    try:
        if time.time() > float(exp):
            return False
    except ValueError:
        return False
    expected = _hmac.new(secret.encode(), f"{path}:{exp}".encode(), hashlib.sha256).hexdigest()
    return _hmac.compare_digest(expected, sig)

def _sign_result_url(result: dict) -> dict:
    """Sign /audio/ and /dash/ URLs in a stream result before sending to the client."""
    url = result.get("url", "")
    parsed = urlparse(url)
    if parsed.path.startswith(("/audio/", "/dash/")):
        signed_path = _sign_path(parsed.path)
        return {**result, "url": f"{parsed.scheme}://{parsed.netloc}{signed_path}"}
    return result

# ---------------------------------------------------------------------------
# Auth failure rate limiter
# ---------------------------------------------------------------------------
_AUTH_FAIL_WINDOW = 60   # seconds
_AUTH_FAIL_LIMIT  = 10   # max failures per window per IP
_auth_fails: dict[str, list[float]] = {}

def _client_ip(request: Request) -> str:
    """Best-effort client IP, preferring Cloudflare's header."""
    return (
        request.headers.get("CF-Connecting-IP")
        or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )

def _auth_rate_ok(ip: str) -> bool:
    """Return False when IP has exceeded the failure threshold."""
    now = time.time()
    window_start = now - _AUTH_FAIL_WINDOW
    hits = [t for t in _auth_fails.get(ip, []) if t > window_start]
    _auth_fails[ip] = hits
    return len(hits) < _AUTH_FAIL_LIMIT

def _record_auth_fail(ip: str) -> None:
    now = time.time()
    _auth_fails.setdefault(ip, []).append(now)
    # Evict stale IPs so the dict doesn't grow forever on long-running containers
    if len(_auth_fails) > 5000:
        cutoff = now - _AUTH_FAIL_WINDOW
        stale = [k for k, v in _auth_fails.items() if not v or max(v) < cutoff]
        for k in stale:
            _auth_fails.pop(k, None)

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing configured music providers...")
    if deezer.is_configured():
        dz_ok = await deezer.health()
        logger.info(f"Deezer status: {'Ready' if dz_ok else 'Authentication failed'}")
    else:
        logger.warning("Deezer ARL not set. Deezer fallback disabled.")

    if tidal.is_configured():
        td_ok = await tidal.health()
        logger.info(f"Tidal status: {'Ready' if td_ok else 'Unreachable'}")
    else:
        logger.info("Tidal token.json not found. Run python app/auth_tidal.py to enable Tidal.")

    logger.info(f"Preferred provider: {settings.preferred_provider.upper()} | Fallback: {settings.enable_fallback} | Diag routes: {settings.diag_enabled}")
    yield

    # Close HTTP clients on shutdown to avoid connection leaks
    await deezer.client.aclose()
    await tidal.client.aclose()

app = FastAPI(
    title="Unified Lossless Addon",
    version="2.2.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots():
    """Instruct automated crawlers and search indexers to ignore all routes."""
    return PlainTextResponse("User-agent: *\nDisallow: /\n")

@app.middleware("http")
async def security_token_middleware(request: Request, call_next):
    """
    Validate access token for BitChord and Eclipse Music and reject unauthorized callers.
    Supports:
    - CORS OPTIONS preflight: allowed without credentials per standard
    - Path-based access token: https://host/{token}/manifest.json (BitChord format)
    - Query parameter token: https://host/manifest.json?token={token} (Eclipse Music format)
    - Headers: Authorization: Bearer {token} or X-Access-Token: {token}
    """
    # W3C CORS preflight requests do not carry credentials/tokens; let OPTIONS pass to CORS middleware
    if request.method == "OPTIONS":
        return await call_next(request)

    token = settings.access_token
    path = request.url.path
    ip = _client_ip(request)

    # Extract Bearer token safely if present
    auth_header = request.headers.get("Authorization", "").strip()
    bearer_token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""

    # /diag/* routes require DIAG_ENABLED=true: they proxy your Bearer token to Tidal
    if path.startswith("/diag/"):
        if not settings.diag_enabled:
            return JSONResponse(
                status_code=403,
                content={"error": "Diagnostic routes are disabled. Set DIAG_ENABLED=true in .env to enable."}
            )
        q_tok = request.query_params.get("token", "")
        h_tok = request.headers.get("X-Access-Token", "")
        if token and not (
            _hmac.compare_digest(q_tok, token)
            or _hmac.compare_digest(h_tok, token)
            or (bearer_token and _hmac.compare_digest(bearer_token, token))
        ):
            _record_auth_fail(ip)
            return JSONResponse(status_code=401, content={"error": "Unauthorized."})
        return await call_next(request)

    if path in ("/", "/robots.txt", "/favicon.ico", "/health"):
        return await call_next(request)

    # Open mode when no ACCESS_TOKEN configured
    if not token:
        return await call_next(request)

    # Audio/DASH proxy routes are authenticated by short-lived HMAC signature, not ACCESS_TOKEN.
    # BitChord and Eclipse Music fetch these URLs directly using the signed URL returned from /stream/ —
    # they do not add the path-prefix token on this second request.
    if path.startswith(("/audio/", "/dash/")):
        exp = request.query_params.get("exp")
        sig = request.query_params.get("sig")
        if not _verify_path_sig(path, exp, sig):
            _record_auth_fail(ip)
            return JSONResponse(status_code=401, content={"error": "Unauthorized."})
        return await call_next(request)

    # Check rate limit before evaluating the token so brute-forcers get cut off fast
    if not _auth_rate_ok(ip):
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": str(_AUTH_FAIL_WINDOW)},
            content={"error": "Too many failed attempts. Try again later."},
        )

    # Path-prefix token: https://host/{token}/manifest.json
    token_prefix = f"/{token}"
    if path == token_prefix or path.startswith(f"{token_prefix}/"):
        stripped_path = path[len(token_prefix):]
        if not stripped_path:
            stripped_path = "/"
        request.scope["path"] = stripped_path
        return await call_next(request)

    # Constant-time comparison prevents timing oracle on query/header/bearer token
    q_tok = request.query_params.get("token", "")
    h_tok = request.headers.get("X-Access-Token", "")
    if (
        _hmac.compare_digest(q_tok, token)
        or _hmac.compare_digest(h_tok, token)
        or (bearer_token and _hmac.compare_digest(bearer_token, token))
    ):
        return await call_next(request)

    _record_auth_fail(ip)
    return JSONResponse(
        status_code=401,
        content={"error": "Unauthorized: valid access token required."}
    )


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Interactive status landing page for homelab and portfolio display."""
    dz_configured = deezer.is_configured()
    td_configured = tidal.is_configured()

    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    raw_host = request.headers.get("x-forwarded-host", request.headers.get("host", str(request.url.netloc)))
    # Strip any injected paths or query strings from the host header
    safe_netloc = urlparse(f"x://{raw_host}").netloc or raw_host.split("/")[0]
    public_host = settings.public_host or f"{proto}://{safe_netloc}".rstrip("/")

    with open("app/templates/dashboard.html", "r", encoding="utf-8") as f:
        html = f.read()

    rendered = html.replace(
        "{{preferred_provider}}", settings.preferred_provider.upper()
    ).replace(
        "{{fallback_status}}", "Active (Deezer FLAC fallback)" if settings.enable_fallback else "Disabled"
    ).replace(
        "{{public_host}}", public_host
    ).replace(
        "{{dz_indicator_class}}", "indicator-active" if dz_configured else "indicator-idle"
    ).replace(
        "{{dz_status_label}}", "ONLINE" if dz_configured else "STANDBY"
    ).replace(
        "{{dz_status_class}}", "status-active" if dz_configured else "status-idle"
    ).replace(
        "{{dz_status_text}}", "Active and operational" if dz_configured else "Awaiting DEEZER_ARL in environment"
    ).replace(
        "{{td_indicator_class}}", "indicator-active" if td_configured else "indicator-idle"
    ).replace(
        "{{td_status_label}}", "ONLINE" if td_configured else "STANDBY"
    ).replace(
        "{{td_status_class}}", "status-active" if td_configured else "status-idle"
    ).replace(
        "{{td_status_text}}", "Active and operational" if td_configured else "Awaiting token.json (run auth_tidal.py)"
    )
    return HTMLResponse(content=rendered)

@app.get("/manifest.json", response_model=ManifestResponse)
async def manifest():
    """Dual BitChord & Eclipse Music addon discovery contract."""
    return {
        "id": "unified-lossless-homelab",
        "name": "Homelab HiFi",
        "version": "2.2.0",
        "description": "Dual BitChord & Eclipse Music Lossless Addon",
        "resources": ["search", "stream", "isrc", "resolve"],
        "types": ["track", "album", "artist"],
        "contentType": "music",
        "settings": [
            {
                "key": "quality",
                "default": "lossless",
                "options": [
                    {"value": "lossless"},
                    {"value": "high"},
                    {"value": "low"}
                ]
            }
        ]
    }

def sanitize_search_query(query: str) -> str:
    """Normalize query strings to avoid search engine dead-ends with connector words."""
    cleaned = re.sub(r'[\(\[]\s*(?:feat|ft|with|featuring)\b\.?\s*([^\]\)]+)[\)\]]', r' \1 ', query.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r'\b(?:feat|ft|with|featuring)\b\.?', ' ', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'[\(\[]\s*[\)\]]', ' ', cleaned)
    return re.sub(r'\s+', ' ', cleaned).strip()

def score_track_relevance(track: dict, query: str, is_preferred: bool) -> float:
    """Score track relevance to query for multi-provider interleaving and deduplication."""
    q_lower = query.lower().strip()
    q_clean = re.sub(r'[^\w\s]', ' ', q_lower)
    q_words = [w for w in q_clean.split() if w]
    if not q_words:
        return 0.0

    version_keywords = {"remix", "mix", "dub", "edit", "version", "acoustic", "instrumental", "live", "feat", "ft"}
    content_words = [w for w in q_words if w not in version_keywords]
    format_query_words = [w for w in q_words if w in version_keywords]

    title = track.get("title", "").strip().lower()
    artist = track.get("artist", "").strip().lower()
    core_title = re.sub(r'[\(\[][^\)\]]*[\)\]]', '', title).strip()

    title_words = set(re.sub(r'[^\w\s]', ' ', title).split())
    artist_words = set(re.sub(r'[^\w\s]', ' ', artist).split())

    matched_title_content = sum(1 for w in content_words if w in title_words)
    matched_artist_content = sum(1 for w in content_words if w in artist_words)

    # Zero-relevance check: must match at least one content word if content_words exist
    if content_words and matched_title_content == 0 and matched_artist_content == 0:
        return -1.0

    score = (matched_title_content * 40.0) + (matched_artist_content * 40.0)

    # Whole title phrase match or starts-with bonus
    if core_title == q_lower or title == q_lower:
        score += 100.0
    elif core_title in q_lower or q_lower.startswith(core_title):
        score += 80.0

    if artist and (artist in q_lower or q_lower.endswith(artist)):
        score += 60.0

    # Synergy: matches both title and artist
    if matched_title_content > 0 and matched_artist_content > 0:
        score += 80.0

    # Specific version & mix alignment:
    has_mix = any(k in title for k in ("remix", "mix", "dub", "edit", "version"))
    wants_version = bool(format_query_words) or any(
        w in ("tak", "tik", "alarm", "solo", "club", "reverk", "acoustic", "live") for w in q_words
    )

    if wants_version:
        spec_matches = sum(1 for w in q_words if w in title_words)
        score += spec_matches * 30.0
        if not has_mix:
            score -= 40.0
    else:
        if has_mix:
            score -= 100.0

    if is_preferred:
        score += 5.0

    return score

def merge_and_rank_tracks(
    tidal_tracks: list[dict],
    deezer_tracks: list[dict],
    query: str,
    preferred_provider: str = "tidal"
) -> list[dict]:
    """Interleave and rank tracks from multiple providers based on relevance to query."""
    scored_candidates = []
    for t in tidal_tracks:
        s = score_track_relevance(t, query, is_preferred=(preferred_provider == "tidal"))
        if s > 0:
            scored_candidates.append((s, "tidal", t))

    for t in deezer_tracks:
        s = score_track_relevance(t, query, is_preferred=(preferred_provider == "deezer"))
        if s > 0:
            scored_candidates.append((s, "deezer", t))

    scored_candidates.sort(key=lambda x: x[0], reverse=True)

    seen_keys = set()
    final_tracks = []
    for s, prov, t in scored_candidates:
        norm_title = re.sub(r'[^a-z0-9]', '', t.get("title", "").lower())
        norm_artist = re.sub(r'[^a-z0-9]', '', t.get("artist", "").lower())
        key = (norm_title, norm_artist)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        final_tracks.append(t)

    return final_tracks

@app.get("/search", response_model=SearchResponse)
async def search(q: str, quality: str = "lossless"):
    """Multi-provider search aggregation respecting PREFERRED_PROVIDER order with relevance ranking."""
    query = sanitize_search_query(q)
    if not query:
        return {"tracks": [], "results": []}

    tidal_limit = 15 if settings.preferred_provider == "tidal" else 8
    deezer_limit = 15 if settings.preferred_provider == "deezer" else 8

    tasks = []
    if tidal.is_configured():
        tasks.append(tidal.search(query, limit=tidal_limit))
    else:
        tasks.append(asyncio.sleep(0, result=[]))

    if deezer.is_configured():
        tasks.append(deezer.search(query, limit=deezer_limit))
    else:
        tasks.append(asyncio.sleep(0, result=[]))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    tidal_tracks = results[0] if isinstance(results[0], list) else []
    deezer_tracks = results[1] if isinstance(results[1], list) else []

    combined = merge_and_rank_tracks(
        tidal_tracks=tidal_tracks,
        deezer_tracks=deezer_tracks,
        query=query,
        preferred_provider=settings.preferred_provider
    )

    return {"tracks": combined, "results": combined}

@app.get("/resolve-isrc", response_model=ResolveIsrcResponse)
async def resolve_isrc(isrc: str):
    """
    Resolve recording by ISRC code conforming to Eclipse Music addon specification.
    Allows Eclipse to query for an exact recording in ~150ms before fuzzy searching.
    """
    clean_isrc = isrc.strip().upper()
    if not clean_isrc:
        return JSONResponse(status_code=404, content={"trackId": None, "id": None})

    # 1. Check Deezer ISRC (fastest: ~150ms, provides 100% progressive FLAC)
    if deezer.is_configured():
        try:
            dz_url = f"https://api.deezer.com/track/isrc:{clean_isrc}"
            resp = await deezer.client.get(dz_url, timeout=3.0)
            if resp.status_code == 200:
                data = resp.json()
                dz_id = data.get("id")
                if dz_id and not data.get("error"):
                    track_id = f"dz:{dz_id}"
                    logger.info(f"Resolved ISRC {clean_isrc} via Deezer -> {track_id}")
                    return {"trackId": track_id, "id": track_id}
        except Exception as e:
            logger.warning(f"Deezer ISRC lookup error for {clean_isrc}: {e}")

    # 2. Check Tidal ISRC
    if tidal.is_configured():
        try:
            td_tracks = await tidal.search(f"isrc:{clean_isrc}", limit=3)
            for t in td_tracks:
                if (t.get("isrc") or "").upper() == clean_isrc:
                    logger.info(f"Resolved ISRC {clean_isrc} via Tidal -> {t['id']}")
                    return {"trackId": t["id"], "id": t["id"]}
        except Exception as e:
            logger.warning(f"Tidal ISRC lookup error for {clean_isrc}: {e}")

    return JSONResponse(status_code=404, content={"trackId": None, "id": None})

@app.get("/resolve", response_model=ResolveResponse)
async def resolve(
    title: str = "",
    artist: str = "",
    isrc: str | None = None,
    durationMs: int | None = None
):
    """
    Resolve recording identity for Eclipse Music generated queues, radio, and mix shelves.
    """
    # 1. Exact ISRC resolution when known
    if isrc and isrc.strip():
        clean_isrc = isrc.strip().upper()
        if deezer.is_configured():
            try:
                dz_url = f"https://api.deezer.com/track/isrc:{clean_isrc}"
                resp = await deezer.client.get(dz_url, timeout=3.0)
                if resp.status_code == 200:
                    data = resp.json()
                    dz_id = data.get("id")
                    if dz_id and not data.get("error"):
                        return {
                            "item": {
                                "id": f"dz:{dz_id}",
                                "type": "track",
                                "title": data.get("title", title),
                                "artist": data.get("artist", {}).get("name", artist),
                                "isrc": clean_isrc
                            }
                        }
            except Exception as e:
                logger.warning(f"Deezer resolve ISRC error for {clean_isrc}: {e}")

    # 2. Multi-provider search and canonical ranking
    q = f"{artist} {title}".strip()
    query = sanitize_search_query(q)
    if not query:
        return {"item": None}

    search_result = await search(q=query)
    tracks = search_result.get("tracks", [])
    if not tracks:
        return {"item": None}

    best = tracks[0]
    best_isrc = getattr(best, "isrc", None) or (best.get("isrc") if isinstance(best, dict) else None)
    best_id = getattr(best, "id", None) or (best.get("id") if isinstance(best, dict) else "")
    best_title = getattr(best, "title", None) or (best.get("title") if isinstance(best, dict) else title)
    best_artist = getattr(best, "artist", None) or (best.get("artist") if isinstance(best, dict) else artist)

    return {
        "item": {
            "id": best_id,
            "type": "track",
            "title": best_title,
            "artist": best_artist,
            "isrc": best_isrc
        }
    }

def _needs_progressive_stream(request: Request) -> bool:
    """
    Determine if the requesting client cannot play MPEG-DASH and requires
    a progressive audio stream (such as Deezer FLAC).

    Returns True for:
      - Apple AVPlayer / iOS / macOS clients (AppleCoreMedia, CFNetwork, etc.)
      - Web browser players (Origin / Referer present, or standard browser UA without ExoPlayer/BitChord)
        because browser JS DASH players face cross-origin CORS 403 on Tidal CDN media segments.
      - Explicit ?prefer=progressive or ?client=eclipse/web/apple query params.

    Returns False for:
      - BitChord mobile / ExoPlayer / AndroidX Media3 / OkHttp / Dart clients
        which natively decode MPEG-DASH and have no browser CORS restrictions.
      - Explicit ?prefer=dash or ?client=bitchord/exoplayer query params.
    """
    # 1. Explicit query parameter override
    prefer = request.query_params.get("prefer", "").lower().strip()
    if prefer in ("dash", "native", "direct"):
        return False
    if prefer in ("progressive", "flac", "deezer"):
        return True

    client = request.query_params.get("client", "").lower().strip()
    if client in ("bitchord", "exoplayer", "media3", "android"):
        return False
    if client in ("eclipse", "web", "browser", "apple", "ios"):
        return True

    # 2. Config override
    cfg_mode = getattr(settings, "tidal_dash_fallback_to_deezer", "auto").lower()
    if cfg_mode == "always":
        return True
    if cfg_mode == "never":
        return False

    # 3. Header inspection
    ua = request.headers.get("user-agent", "").lower()
    origin = request.headers.get("origin", "").strip()
    sec_fetch = request.headers.get("sec-fetch-mode", "").lower().strip()

    # Apple AVPlayer cannot decode MPEG-DASH XML manifests
    if any(k in ua for k in ("iphone", "ipad", "ipod", "applecoremedia", "cfnetwork")):
        return True

    # Web browser client (e.g. Eclipse Music web app)
    # Browsers cannot fetch Tidal DASH media segments cross-origin (CORS 403)
    if origin or sec_fetch == "cors":
        return True

    # Native BitChord / ExoPlayer / Media3 / OkHttp / Dart
    if any(k in ua for k in ("exoplayer", "media3", "bitchord", "okhttp", "dalvik", "dart")):
        return False

    # General desktop/mobile browser UA check
    if "mozilla" in ua and any(b in ua for b in ("chrome", "safari", "firefox", "edge")):
        return True

    # Default to native DASH for native clients
    return False

@app.get("/stream/{item_id}", response_model=StreamResponse)
async def resolve_stream(item_id: str, request: Request, quality: str = "lossless"):
    """Resolve stream URL by namespace prefix with cross-provider fallback."""
    quality = "lossless"
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    raw_host = request.headers.get("x-forwarded-host", request.headers.get("host", str(request.url.netloc)))
    safe_netloc = urlparse(f"x://{raw_host}").netloc or raw_host.split("/")[0]
    dynamic_host = settings.public_host or f"{proto}://{safe_netloc}".rstrip("/")

    # BitChord sends title/artist as query params for the stream endpoint.
    # These are used as hints if Tidal metadata fetch fails (e.g. regional tracks).
    hint_title = request.query_params.get("title", "").strip()
    hint_artist = request.query_params.get("artist", "").strip()

    needs_progressive = _needs_progressive_stream(request)

    try:
        result = await asyncio.wait_for(
            _resolve_stream_inner(
                item_id,
                quality,
                dynamic_host,
                hint_title,
                hint_artist,
                needs_progressive=needs_progressive,
            ),
            timeout=7.0
        )
        if isinstance(result, dict) and result.get("url"):
            result = _sign_result_url(result)
        return result
    except asyncio.TimeoutError:
        logger.warning(f"Stream resolution timed out after 7s for {item_id}; returning 404 fast")
        return JSONResponse(
            status_code=404,
            content={"error": f"Track {item_id} resolution timed out. Falling back to YouTube Music."}
        )

async def _resolve_stream_inner(
    item_id: str,
    quality: str,
    dynamic_host: str,
    hint_title: str = "",
    hint_artist: str = "",
    needs_progressive: bool = False,
    is_apple_client: bool | None = None,
):
    """Inner resolution logic, wrapped by resolve_stream with a hard 7s timeout."""
    if is_apple_client is not None:
        needs_progressive = needs_progressive or is_apple_client

    if item_id.startswith("td:"):
        # Pre-launch Deezer fallback concurrently with tidal.get_stream().
        # Tidal now returns None fast for HIGH-only tracks, so Deezer result
        # is typically ready the moment Tidal fails — zero extra latency.
        tidal_task = asyncio.create_task(
            tidal.get_stream(item_id, quality, public_host=dynamic_host)
        )
        deezer_prefetch = None
        if settings.enable_fallback and deezer.is_configured():
            deezer_prefetch = asyncio.create_task(
                _tidal_to_deezer_fallback(item_id, quality, hint_title, hint_artist)
            )

        res = await tidal_task
        if res and res.get("url"):
            # Tidal DASH manifests require client-side demuxing and segment fetching from Tidal's CDN.
            # Apple AVPlayer cannot decode DASH manifests; Web browsers fail on cross-origin segment fetch (CORS 403).
            # ExoPlayer / BitChord natively plays DASH via patched init segments.
            # Only redirect to progressive Deezer FLAC if the client requires progressive audio.
            if res.get("manifest") == "dash" and needs_progressive and deezer_prefetch:
                fallback_stream = await deezer_prefetch
                if fallback_stream and fallback_stream.get("url"):
                    logger.info(f"Tidal track {item_id} returned DASH manifest; delivering progressive Deezer FLAC for browser/Apple client compatibility")
                    return fallback_stream
            elif deezer_prefetch:
                deezer_prefetch.cancel()
            return res

        if deezer_prefetch:
            logger.info(f"Tidal track {item_id} has no FLAC; awaiting parallel Deezer fallback")
            fallback_stream = await deezer_prefetch
            if fallback_stream:
                return fallback_stream

    if item_id.startswith("dz:"):
        # Deezer is the primary provider with near-100% FLAC availability.
        # Resolve Deezer first; only invoke Tidal fallback if Deezer explicitly fails.
        res = await deezer.get_stream(item_id, quality)
        if res and res.get("url"):
            return res

        if settings.enable_fallback and tidal.is_configured():
            logger.info(f"Deezer track {item_id} failed; executing Tidal fallback")
            fallback_stream = await _deezer_to_tidal_fallback(item_id, quality, dynamic_host)
            if fallback_stream:
                return fallback_stream

    return JSONResponse(
        status_code=404,
        content={"error": f"Track {item_id} is not available in lossless FLAC. Falling back to YouTube Music."}
    )

def _clean_title_for_search(title: str, preserve_version: bool = False, preserve_remix: bool = False) -> str:
    """Clean track title for fallback search across providers.

    If preserve_version (or preserve_remix) is True, keeps version/remix/mix/feat suffixes
    while stripping extraneous master metadata (e.g. Remastered, Explicit, Deluxe Edition).
    If False, strips all version, collaborator, and remix suffixes down to canonical core song title.
    """
    keep_version = preserve_version or preserve_remix

    if keep_version:
        cleaned = re.sub(
            r'\s*[\(\[](?:remaster(?:ed)?(?:[^\)\]]*)?|deluxe(?: edition)?|bonus track|explicit|clean|anniversary(?:[^\)\]]*)?)[\)\]]',
            '', title, flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r'\s*-\s*(?:remaster(?:ed)?(?: \d+)?|deluxe(?: edition)?|anniversary).*$',
            '', cleaned, flags=re.IGNORECASE,
        )
        return re.sub(r'\s+', ' ', cleaned).strip()

    # Base canonical title cleaning: remove collaborator credits (feat/ft/with)
    cleaned = re.sub(r'\s*[\(\[](?:feat|ft|with)\.?[^\)\]]*[\)\]]', '', title, flags=re.IGNORECASE)
    # Strip all version, mix, edit, and edition suffixes
    cleaned = re.sub(
        r'\s*[\(\[][^\)\]]*(?:remix|mix|dub|edit|version|live|acoustic|instrumental|clean|explicit|deluxe|bonus|remaster)[^\)\]]*[\)\]]',
        '', cleaned, flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r'\s*-\s*(?:radio edit|extended mix|single version|solo version|remix|mix|dub|remaster(?:ed)?(?: \d+)?|live|acoustic|deluxe).*$',
        '', cleaned, flags=re.IGNORECASE,
    )
    return re.sub(r'\s+', ' ', cleaned).strip()


async def _tidal_to_deezer_fallback(
    item_id: str,
    quality: str,
    hint_title: str = "",
    hint_artist: str = "",
) -> dict | None:
    """Search Deezer by track title+artist for a Tidal track that failed to resolve.

    Preserves mix, version, and featuring details so the exact version chosen by
    the user is resolved on Deezer. Falls back to base canonical search only if
    the version-specific match is unavailable.
    """
    clean_id = item_id.replace("td:", "")
    target_title = ""
    artist = ""

    # Short-circuit: if hints were provided by the caller, use them immediately
    if hint_title and hint_artist:
        target_title = hint_title
        artist = hint_artist
        logger.debug(f"Direct hint metadata for Deezer fallback: title={target_title!r}, artist={artist!r}")
    else:
        from app.providers.tidal import API_BASE
        try:
            meta_resp = await tidal.client.get(
                f"{API_BASE}/tracks/{clean_id}",
                headers=tidal._auth_headers(),
                params={"countryCode": tidal.country_code}
            )
            if meta_resp.status_code == 200:
                meta = meta_resp.json()
                t_title = meta.get("title", "").strip()
                t_version = (meta.get("version") or "").strip()
                target_title = f"{t_title} ({t_version})" if t_version and t_version.lower() not in t_title.lower() else t_title

                artists_list = [meta.get("artist", {}).get("name", "")]
                for a in meta.get("artists", []):
                    a_name = a.get("name")
                    if a_name and a_name not in artists_list:
                        artists_list.append(a_name)
                artist = ", ".join(artists_list).strip()
        except Exception as exc:
            logger.warning(f"Could not fetch Tidal metadata for {clean_id}: {exc}")

    if not target_title or not artist:
        logger.warning(f"No title/artist available for Deezer fallback on {clean_id}")
        return None

    # Two-tier resolution:
    # Tier 1: Targeted search preserving mix / version / featuring
    # Tier 2: Canonical base title search (only if Tier 1 yields no stream)
    queries_to_try = []
    has_version_hint = bool(re.search(r'\b(?:remix|mix|dub|edit|version|acoustic|instrumental|live|feat|ft)\b', target_title, re.IGNORECASE))
    if has_version_hint:
        targeted_clean = _clean_title_for_search(target_title, preserve_version=True)
        queries_to_try.append(sanitize_search_query(f"{targeted_clean} {artist}"))

    base_clean = _clean_title_for_search(target_title, preserve_version=False)
    base_query = sanitize_search_query(f"{base_clean} {artist}")
    if base_query not in queries_to_try:
        queries_to_try.append(base_query)

    for q in queries_to_try:
        logger.debug(f"Attempting Deezer fallback with query: {q!r}")
        dz_matches = await deezer.search(q, limit=3)
        for match in dz_matches:
            stream = await deezer.get_stream(match["id"], quality)
            if stream and stream.get("url"):
                logger.info(f"Deezer fallback resolved {q!r} to {match['id']}")
                return stream

    return None

async def _deezer_to_tidal_fallback(item_id: str, quality: str, dynamic_host: str) -> dict | None:
    """Fetch track title+artist from Deezer public API, then search Tidal for cross-provider fallback."""
    clean_id = item_id.replace("dz:", "")
    try:
        meta_resp = await deezer.client.get(f"https://api.deezer.com/track/{clean_id}")
        if meta_resp.status_code != 200:
            return None
        meta = meta_resp.json()
        title = meta.get("title", "").strip()
        title_version = (meta.get("title_version") or "").strip()
        target_title = f"{title} ({title_version})" if title_version and title_version.lower() not in title.lower() else title
        artist = meta.get("artist", {}).get("name", "").strip()
        if not target_title or not artist:
            return None
    except Exception as exc:
        logger.warning(f"Could not fetch Deezer metadata for {clean_id}: {exc}")
        return None

    queries_to_try = []
    has_version_hint = bool(re.search(r'\b(?:remix|mix|dub|edit|version|acoustic|instrumental|live|feat|ft)\b', target_title, re.IGNORECASE))
    if has_version_hint:
        targeted_clean = _clean_title_for_search(target_title, preserve_version=True)
        queries_to_try.append(sanitize_search_query(f"{targeted_clean} {artist}"))

    base_clean = _clean_title_for_search(target_title, preserve_version=False)
    base_query = sanitize_search_query(f"{base_clean} {artist}")
    if base_query not in queries_to_try:
        queries_to_try.append(base_query)

    for q in queries_to_try:
        logger.debug(f"Attempting Tidal fallback with query: {q!r}")
        td_matches = await tidal.search(q, limit=3)
        for match in td_matches:
            stream = await tidal.get_stream(match["id"], quality, public_host=dynamic_host)
            if stream and stream.get("url"):
                logger.info(f"Tidal fallback resolved {q!r} to {match['id']}")
                return stream

    return None

@app.get("/diag/proxy")
async def diag_proxy(request: Request, path: str):
    """Proxy authenticated diagnostic query to Tidal API."""
    qp = dict(request.query_params)
    qp.pop("path", None)
    cc = qp.pop("country", None) or qp.get("countryCode") or tidal.country_code
    qp["countryCode"] = cc
    url = f"https://api.tidal.com/v1/{path.lstrip('/')}"
    resp = await tidal.client.get(url, headers=tidal._auth_headers(), params=qp)
    return Response(content=resp.text, media_type="application/json")

@app.get("/diag/search")
async def diag_search(q: str, country: str = ""):
    """Diagnostic endpoint to test Tidal search without authentication."""
    old_cc = tidal.country_code
    if country:
        tidal.country_code = country
    try:
        results = await tidal.search(q, limit=10)
        return {"activeCountry": tidal.country_code, "count": len(results), "tracks": results}
    finally:
        tidal.country_code = old_cc

@app.get("/diag/stream/{item_id}")
async def diag_stream(item_id: str, request: Request, country: str = ""):
    """Diagnostic endpoint to test track stream resolution without authentication."""
    old_cc = tidal.country_code
    if country:
        tidal.country_code = country
    try:
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("x-forwarded-host", request.headers.get("host", str(request.url.netloc)))
        dynamic_host = f"{proto}://{host}".rstrip("/")
        stream = await tidal.get_stream(item_id, "lossless", public_host=dynamic_host)
        return {"activeCountry": tidal.country_code, "stream": stream}
    finally:
        tidal.country_code = old_cc

@app.get("/diag/td/{track_id}")
async def diag_tidal(track_id: str, country: str = ""):
    """Diagnostic endpoint to inspect raw Tidal manifest data."""
    clean_id = track_id.replace("td:", "").replace(".mpd", "")
    cc = country or tidal.country_code
    out = {"countryCode": cc}
    for q in ("HI_RES_LOSSLESS", "LOSSLESS", "HIGH"):
        url = f"https://api.tidal.com/v1/tracks/{clean_id}/playbackinfopostpaywall"
        params = {
            "countryCode": cc,
            "audioquality": q,
            "playbackmode": "STREAM",
            "assetpresentation": "FULL",
        }
        try:
            resp = await tidal.client.get(url, headers=tidal._auth_headers(), params=params)
            if resp.status_code == 200:
                d = resp.json()
                raw_m = d.get("manifest", "")
                mime = d.get("manifestMimeType", "")
                decoded = ""
                try:
                    decoded = base64.b64decode(raw_m).decode("utf-8")
                except Exception as e:
                    decoded = f"decode error: {e}"
                out[q] = {
                    "status": 200,
                    "mime": mime,
                    "bitDepth": d.get("bitDepth"),
                    "sampleRate": d.get("sampleRate"),
                    "audioQuality": d.get("audioQuality"),
                    "manifest_preview": decoded[:600] if decoded else "",
                }
            else:
                out[q] = {"status": resp.status_code, "text": resp.text[:200]}
        except Exception as exc:
            out[q] = {"error": str(exc)}
    return out

@app.get("/dash/td/{track_id}/init.mp4")
async def serve_dash_init(track_id: str, request: Request):
    """Serve patched DASH fMP4 initialization segment with valid non-zero sampleRate for ExoPlayer."""
    if not _verify_path_sig(request.url.path, request.query_params.get("exp"), request.query_params.get("sig")):
        return JSONResponse(status_code=401, content={"error": "Unauthorized."})
    clean_id = track_id.replace("td:", "").replace(".mpd", "").replace("/init.mp4", "")
    patched_init = tidal.init_segment_cache.get(clean_id)
    if not patched_init:
        init_url = tidal.init_url_cache.get(clean_id)
        if not init_url:
            await tidal.get_stream(clean_id)
            init_url = tidal.init_url_cache.get(clean_id)
        if init_url:
            try:
                resp = await tidal.client.get(init_url)
                if resp.status_code == 200:
                    raw_bytes = bytearray(resp.content)
                    flac_pos = raw_bytes.find(b"fLaC")
                    if flac_pos != -1 and len(raw_bytes) >= flac_pos + 32:
                        rate_pos = flac_pos + 28
                        if raw_bytes[rate_pos:rate_pos+4] == b"\x00\x00\x00\x00":
                            # Patch 16.16 fixed point sample rate to 44.1kHz (0xAC440000)
                            raw_bytes[rate_pos:rate_pos+4] = b"\xAC\x44\x00\x00"
                    patched_init = bytes(raw_bytes)
                    tidal.init_segment_cache[clean_id] = patched_init
            except Exception as e:
                logger.error(f"Failed to fetch and patch init segment for track {clean_id}: {e}")

    if not patched_init:
        raise HTTPException(status_code=404, detail="Init segment not found")

    return Response(
        content=patched_init,
        media_type="audio/mp4",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Content-Length": str(len(patched_init)),
            "Accept-Ranges": "bytes",
        }
    )

@app.get("/dash/td/{track_id}")
async def serve_dash_manifest(track_id: str, request: Request):
    """Serve decoded DASH MPD XML manifest for BitChord / ExoPlayer."""
    if not _verify_path_sig(request.url.path, request.query_params.get("exp"), request.query_params.get("sig")):
        return JSONResponse(status_code=401, content={"error": "Unauthorized."})

    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    raw_host = request.headers.get("x-forwarded-host", request.headers.get("host", str(request.url.netloc)))
    safe_netloc = urlparse(f"x://{raw_host}").netloc or raw_host.split("/")[0]
    base_origin = settings.public_host or f"{proto}://{safe_netloc}".rstrip("/")

    clean_id = track_id.replace("td:", "").replace(".mpd", "")
    manifest_xml = tidal.dash_cache.get(clean_id)
    if not manifest_xml:
        await tidal.get_stream(clean_id, public_host=base_origin)
        manifest_xml = tidal.dash_cache.get(clean_id)

    if not manifest_xml:
        raise HTTPException(status_code=404, detail="DASH manifest not found")

    # Re-sign the init segment URL embedded in the MPD so ExoPlayer can fetch it
    init_pattern = re.compile(r'initialization="([^"]*?/dash/td/[^"]*?/init\.mp4)"')
    def _re_sign_init(m: re.Match) -> str:
        parsed_init = urlparse(m.group(1))
        signed_path = _sign_path(parsed_init.path)
        origin = f"{parsed_init.scheme}://{parsed_init.netloc}" if parsed_init.netloc else base_origin
        # & must be &amp; inside XML attribute values; parsers unescape before fetching
        signed_url = f"{origin}{signed_path}".replace("&", "&amp;")
        return f'initialization="{signed_url}"'
    manifest_xml = init_pattern.sub(_re_sign_init, manifest_xml)

    return Response(content=manifest_xml, media_type="application/dash+xml")

@app.get("/audio/dz/{track_id}")
async def stream_deezer_flac(track_id: str, request: Request):
    """Proxy encrypted Deezer CDN stream and decrypt chunks on the fly."""
    if not _verify_path_sig(request.url.path, request.query_params.get("exp"), request.query_params.get("sig")):
        return JSONResponse(status_code=401, content={"error": "Unauthorized."})

    clean_id = track_id.replace("dz:", "").replace(".flac", "").replace(".mp3", "")
    range_header = request.headers.get("Range")

    try:
        audio_gen, headers, status = await deezer.stream_decrypted_audio(clean_id, range_header)
        return StreamingResponse(audio_gen, status_code=status, headers=headers)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Error streaming track {clean_id}: {exc}")
        raise HTTPException(status_code=502, detail="Failed to retrieve audio stream from Deezer")

@app.get("/health")
async def health():
    """Health check for Docker and load balancers."""
    return {"status": "ok"}
