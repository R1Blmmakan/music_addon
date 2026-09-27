import re
import base64
import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse, PlainTextResponse
from app.config import settings
from app.providers.deezer import DeezerProvider
from app.providers.tidal import TidalProvider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("bitchord.unified")

deezer = DeezerProvider(settings.deezer_arl, settings.public_host)
tidal = TidalProvider(settings.tidal_token_file, settings.tidal_country_code, settings.public_host)

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
    title="BitChord Unified Lossless Addon",
    version="3.0.0",
    lifespan=lifespan
)

@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots():
    """Instruct automated crawlers and search indexers to ignore all routes."""
    return PlainTextResponse("User-agent: *\nDisallow: /\n")

@app.middleware("http")
async def security_token_middleware(request: Request, call_next):
    """
    Validate path-based access token for BitChord and reject unauthorized callers.
    BitChord natively supports URLs in the form: https://host/{token}/manifest.json.
    """
    token = settings.access_token
    path = request.url.path

    # /diag/* routes require DIAG_ENABLED=true: they proxy your Bearer token to Tidal
    if path.startswith("/diag/"):
        if not settings.diag_enabled:
            return JSONResponse(
                status_code=403,
                content={"error": "Diagnostic routes are disabled. Set DIAG_ENABLED=true in .env to enable."}
            )
        if token and request.query_params.get("token") != token and request.headers.get("X-Access-Token") != token:
            return JSONResponse(status_code=401, content={"error": "Unauthorized."})
        return await call_next(request)

    if path in ("/", "/robots.txt", "/favicon.ico", "/health") or path.startswith("/audio/") or path.startswith("/dash/"):
        return await call_next(request)

    # Open mode when no ACCESS_TOKEN configured
    if not token:
        return await call_next(request)

    # Path-prefix token: https://host/{token}/manifest.json
    token_prefix = f"/{token}"
    if path == token_prefix or path.startswith(f"{token_prefix}/"):
        stripped_path = path[len(token_prefix):]
        if not stripped_path:
            stripped_path = "/"
        request.scope["path"] = stripped_path
        return await call_next(request)

    if (
        request.query_params.get("token") == token
        or request.headers.get("X-Access-Token") == token
    ):
        return await call_next(request)

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
    host = request.headers.get("x-forwarded-host", request.headers.get("host", str(request.url.netloc)))
    public_host = settings.public_host or f"{proto}://{host}".rstrip("/")

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

@app.get("/manifest.json")
async def manifest():
    """BitChord addon discovery contract."""
    return {
        "id": "unified-lossless-homelab",
        "name": "Homelab HiFi (Tidal + Deezer)",
        "version": "1.0.0",
        "resources": ["search", "stream"],
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

@app.get("/search")
async def search(q: str, quality: str = "lossless"):
    """Multi-provider search aggregation respecting PREFERRED_PROVIDER order."""
    query = q.strip()
    if not query:
        return {"tracks": []}

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

    if settings.preferred_provider == "deezer":
        combined = deezer_tracks + tidal_tracks
    else:
        combined = tidal_tracks + deezer_tracks

    return {"tracks": combined}

@app.get("/stream/{item_id}")
async def resolve_stream(item_id: str, request: Request, quality: str = "lossless"):
    """Resolve stream URL by namespace prefix with cross-provider fallback."""
    quality = "lossless"
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", str(request.url.netloc)))
    dynamic_host = f"{proto}://{host}".rstrip("/")

    # BitChord sends title/artist as query params for the stream endpoint.
    # These are used as hints if Tidal metadata fetch fails (e.g. regional tracks).
    hint_title = request.query_params.get("title", "").strip()
    hint_artist = request.query_params.get("artist", "").strip()

    try:
        result = await asyncio.wait_for(
            _resolve_stream_inner(item_id, quality, dynamic_host, hint_title, hint_artist),
            timeout=7.0
        )
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
):
    """Inner resolution logic, wrapped by resolve_stream with a hard 7s timeout."""
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
            if deezer_prefetch:
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

def _clean_title_for_search(title: str) -> str:
    """Strip parenthetical and version suffixes that cause Deezer to return remixes over originals.

    Keeps the core title while removing:
      (feat. ...), [feat. ...], (Radio Edit), (Extended), (Remix), etc.
    """
    # Remove feat./ft./with collaborator credits in parens or brackets
    title = re.sub(r'\s*[\(\[](?:feat|ft|with)\.?[^\)\]]*[\)\]]', '', title, flags=re.IGNORECASE)
    # Remove common version/edition suffixes in parens or brackets
    title = re.sub(
        r'\s*[\(\[](?:radio edit|extended|extended mix|single version|album version|'
        r'live(?: at [^\)\]]*)?|remaster(?:ed)?(?:[^\)\]]*)?|acoustic|instrumental|clean|explicit|'
        r'deluxe(?: edition)?|bonus track)[\)\]]',
        '', title, flags=re.IGNORECASE,
    )
    # Remove trailing hyphenated version info like " - Radio Edit", " - Remastered 2011"
    title = re.sub(
        r'\s*-\s*(?:radio edit|extended mix|single version|remaster(?:ed)?(?: \d+)?|live|acoustic|deluxe).*$',
        '', title, flags=re.IGNORECASE,
    )
    return title.strip()


async def _tidal_to_deezer_fallback(
    item_id: str,
    quality: str,
    hint_title: str = "",
    hint_artist: str = "",
) -> dict | None:
    """Search Deezer by track title+artist for a Tidal track that failed to resolve.

    If hint_title and hint_artist are provided (from request query params), we bypass
    the Tidal metadata API round-trip entirely, shaving ~350ms off fallback latency.
    Otherwise, we fetch metadata from Tidal.
    """
    clean_id = item_id.replace("td:", "")
    query = ""

    # Short-circuit: if hints were provided by the caller, use them immediately
    if hint_title and hint_artist:
        query = f"{_clean_title_for_search(hint_title)} {hint_artist}"
        logger.debug(f"Direct hint query for Deezer fallback: {query!r}")
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
                title = meta.get("title", "").strip()
                artist = meta.get("artist", {}).get("name", "").strip()
                if title and artist:
                    clean = _clean_title_for_search(title)
                    query = f"{clean} {artist}"
        except Exception as exc:
            logger.warning(f"Could not fetch Tidal metadata for {clean_id}: {exc}")

    if not query:
        logger.warning(f"No query available for Deezer fallback on {clean_id}")
        return None

    dz_matches = await deezer.search(query, limit=1)
    for match in dz_matches:
        stream = await deezer.get_stream(match["id"], quality)
        if stream and stream.get("url"):
            logger.info(f"Deezer fallback resolved {query!r} to {match['id']}")
            return stream
    return None

async def _deezer_to_tidal_fallback(item_id: str, quality: str, dynamic_host: str) -> dict | None:
    """Fetch track title+artist from Deezer public API, then search Tidal for cross-provider fallback.

    Same issue as Tidal-to-Deezer: searching by raw numeric ID returns garbage.
    """
    clean_id = item_id.replace("dz:", "")
    try:
        meta_resp = await deezer.client.get(f"https://api.deezer.com/track/{clean_id}")
        if meta_resp.status_code != 200:
            return None
        meta = meta_resp.json()
        title = meta.get("title", "").strip()
        artist = meta.get("artist", {}).get("name", "").strip()
        if not title or not artist:
            return None
        query = f"{title} {artist}"
    except Exception as exc:
        logger.warning(f"Could not fetch Deezer metadata for {clean_id}: {exc}")
        return None

    td_matches = await tidal.search(query, limit=3)
    for match in td_matches:
        stream = await tidal.get_stream(match["id"], quality, public_host=dynamic_host)
        if stream and stream.get("url"):
            logger.info(f"Tidal fallback resolved '{query}' to {match['id']}")
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
async def serve_dash_init(track_id: str):
    """Serve patched DASH fMP4 initialization segment with valid non-zero sampleRate for ExoPlayer."""
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
async def serve_dash_manifest(track_id: str):
    """Serve decoded DASH MPD XML manifest for BitChord / ExoPlayer."""
    clean_id = track_id.replace("td:", "").replace(".mpd", "")
    manifest_xml = tidal.dash_cache.get(clean_id)
    if not manifest_xml:
        await tidal.get_stream(clean_id)
        manifest_xml = tidal.dash_cache.get(clean_id)

    if not manifest_xml:
        raise HTTPException(status_code=404, detail="DASH manifest not found")

    return Response(content=manifest_xml, media_type="application/dash+xml")

@app.get("/audio/dz/{track_id}")
async def stream_deezer_flac(track_id: str, request: Request):
    """Proxy encrypted Deezer CDN stream and decrypt chunks on the fly."""
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
    """Health check endpoint for Docker and homelab status monitors."""
    return {
        "status": "healthy",
        "providers": {
            "deezer": {
                "configured": deezer.is_configured(),
                "ready": await deezer.health() if deezer.is_configured() else False
            },
            "tidal": {
                "configured": tidal.is_configured(),
                "ready": await tidal.health() if tidal.is_configured() else False
            }
        }
    }
