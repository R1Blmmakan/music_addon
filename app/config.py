import os
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

class Settings(BaseModel):
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8000"))
    public_host: str = os.getenv("PUBLIC_HOST", "http://localhost:8000").rstrip("/")
    deezer_arl: str = os.getenv("DEEZER_ARL", "").strip()
    tidal_api_url: str = os.getenv("TIDAL_API_URL", "").rstrip("/")
    preferred_provider: str = os.getenv("PREFERRED_PROVIDER", "tidal").lower()
    enable_fallback: bool = os.getenv("ENABLE_FALLBACK", "true").lower() in ("true", "1", "yes")
    access_token: str = os.getenv("ACCESS_TOKEN", "").strip()

settings = Settings()
