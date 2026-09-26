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
tidal = TidalProvider(settings.tidal_token_file, settings.tidal_country_code)

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
    yield

app = FastAPI(
    title="BitChord Unified Lossless Addon",
    version="1.0.0",
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

    # Public diagnostic routes
    if path in ("/", "/robots.txt", "/favicon.ico"):
        return await call_next(request)

    # If no ACCESS_TOKEN is set in .env, run in open mode
    if not token:
        return await call_next(request)

    # Check if path starts with /{token}/ or /{token}
    token_prefix = f"/{token}"
    if path == token_prefix or path.startswith(f"{token_prefix}/"):
        # Strip token prefix so internal route matches normally
        stripped_path = path[len(token_prefix):]
        if not stripped_path:
            stripped_path = "/"
        request.scope["path"] = stripped_path
        return await call_next(request)

    # Check alternative query parameter or header authentication
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
async def dashboard():
    """Interactive status landing page for homelab and portfolio display."""
    dz_configured = deezer.is_configured()
    td_configured = tidal.is_configured()
    
    with open("app/templates/dashboard.html", "r", encoding="utf-8") as f:
        html = f.read()

    rendered = html.replace(
        "{{dz_status_class}}", "dot-active" if dz_configured else "dot-idle"
    ).replace(
        "{{dz_status_text}}", "Active & Ready" if dz_configured else "Awaiting ARL Token in .env"
    ).replace(
        "{{dz_color}}", "#22c55e" if dz_configured else "#eab308"
    ).replace(
        "{{td_status_class}}", "dot-active" if td_configured else "dot-idle"
    ).replace(
        "{{td_status_text}}", "Active & Ready (HiFi/Lossless)" if td_configured else "Awaiting token.json (run python app/auth_tidal.py)"
    ).replace(
        "{{td_color}}", "#22c55e" if td_configured else "#8b949e"
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
    """Multi-provider search aggregation with lossless priority ranking."""
    query = q.strip()
    if not query:
        return {"tracks": []}

    tasks = []
    if tidal.is_configured():
        tasks.append(tidal.search(query, limit=5))
    else:
        tasks.append(asyncio.sleep(0, result=[]))

    # Deezer search is public and requires no credentials
    tasks.append(deezer.search(query, limit=5))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    tidal_tracks = results[0] if isinstance(results[0], list) else []
    deezer_tracks = results[1] if isinstance(results[1], list) else []

    # Priority sorting: place preferred provider first
    combined = []
    if settings.preferred_provider == "tidal":
        combined.extend(tidal_tracks)
        combined.extend(deezer_tracks)
    else:
        combined.extend(deezer_tracks)
        combined.extend(tidal_tracks)

    return {"tracks": combined}

@app.get("/stream/{item_id}")
async def resolve_stream(item_id: str, quality: str = "lossless"):
    """Resolve stream URL by namespace prefix with cross-provider fallback."""
    # Handle Tidal items
    if item_id.startswith("td:"):
        res = await tidal.get_stream(item_id, quality)
        if res and res.get("url"):
            return res

        # Tidal failed: attempt fallback to Deezer if enabled
        if settings.enable_fallback and deezer.is_configured():
            logger.info(f"Tidal track {item_id} resolution failed; attempting Deezer fallback")
            # Extract track metadata or query Deezer
            dz_matches = await deezer.search(item_id.replace("td:", ""), limit=1)
            if dz_matches:
                fallback_stream = await deezer.get_stream(dz_matches[0]["id"], quality)
                if fallback_stream:
                    return fallback_stream

    # Handle Deezer items
    if item_id.startswith("dz:"):
        res = await deezer.get_stream(item_id, quality)
        if res and res.get("url"):
            return res

        # Deezer failed: attempt fallback to Tidal if enabled
        if settings.enable_fallback and tidal.is_configured():
            logger.info(f"Deezer track {item_id} resolution failed; attempting Tidal fallback")
            td_matches = await tidal.search(item_id.replace("dz:", ""), limit=1)
            if td_matches:
                fallback_stream = await tidal.get_stream(td_matches[0]["id"], quality)
                if fallback_stream:
                    return fallback_stream

    return JSONResponse(
        status_code=404,
        content={"error": f"Track {item_id} could not be resolved by configured providers."}
    )

@app.get("/audio/dz/{track_id}")
async def stream_deezer_flac(track_id: str, request: Request):
    """Proxy encrypted Deezer CDN stream and decrypt chunks on the fly."""
    clean_id = track_id.replace("dz:", "").replace(".flac", "").replace(".mp3", "")
    range_header = request.headers.get("Range")

    try:
        audio_gen, headers, status = await deezer.stream_decrypted_audio(clean_id, range_header)
        return StreamingResponse(audio_gen, status_code=status, headers=headers)
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
