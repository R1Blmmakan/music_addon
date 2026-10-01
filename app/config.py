import os
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

class Settings(BaseModel):
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8000"))
    public_host: str = os.getenv("PUBLIC_HOST", "http://localhost:8000").rstrip("/")
    deezer_arl: str = os.getenv("DEEZER_ARL", "").strip()
    tidal_token_file: str = os.getenv("TIDAL_TOKEN_FILE", "data/token.json")
    tidal_country_code: str = os.getenv("TIDAL_COUNTRY_CODE", "ID")
    # Tidal Android/TV OAuth client credentials
    tidal_client_id: str = os.getenv("TIDAL_CLIENT_ID", "fX2JxdmntZWK0ixT")
    tidal_client_secret: str = os.getenv("TIDAL_CLIENT_SECRET", "1Nn9AfDAjxrgJFJbKNWLeAyKGVGmINuXPPLHVXAvxAg=")
    # "deezer" or "tidal" -- controls search result order and primary stream resolution
    preferred_provider: str = os.getenv("PREFERRED_PROVIDER", "tidal").lower()
    enable_fallback: bool = os.getenv("ENABLE_FALLBACK", "true").lower() in ("true", "1", "yes")
    # "auto" (DASH for ExoPlayer/BitChord, progressive FLAC for Web/Apple), "always", or "never"
    tidal_dash_fallback_to_deezer: str = os.getenv("TIDAL_DASH_FALLBACK_TO_DEEZER", "auto").lower()
    access_token: str = os.getenv("ACCESS_TOKEN", "").strip()
    # Separate HMAC secret for signing /audio/ and /dash/ proxy URLs.
    # Falls back to ACCESS_TOKEN when unset — set this only if you want
    # audio URL signing independent from the path-prefix token.
    audio_signing_secret: str = os.getenv("AUDIO_SIGNING_SECRET", "").strip()
    # /diag/* routes are disabled by default: they forward your Bearer token to arbitrary Tidal endpoints
    diag_enabled: bool = os.getenv("DIAG_ENABLED", "false").lower() in ("true", "1", "yes")

settings = Settings()
