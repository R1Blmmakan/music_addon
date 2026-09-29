from pydantic import BaseModel, Field
from typing import Any

class ManifestOption(BaseModel):
    value: str

class ManifestSetting(BaseModel):
    key: str
    default: str
    options: list[ManifestOption]

class ManifestResponse(BaseModel):
    """Dual manifest schema satisfying both BitChord and Eclipse Music specifications."""
    id: str = "unified-lossless-homelab"
    name: str = "Homelab HiFi"
    version: str = "2.2.0"
    description: str = "Dual BitChord & Eclipse Music Lossless Addon"
    resources: list[str] = ["search", "stream", "isrc", "resolve"]
    # Eclipse Music required extensions
    types: list[str] = ["track", "album", "artist"]
    contentType: str = "music"
    # BitChord settings extension
    settings: list[ManifestSetting] = Field(default_factory=list)

class TrackItem(BaseModel):
    """Track item schema containing metadata for both BitChord and Eclipse Music."""
    id: str
    title: str
    artist: str
    album: str = ""
    duration: int = 0
    artworkURL: str | None = None
    artwork: str | None = None
    format: str = "flac"
    audioQuality: str = "LOSSLESS"
    bitrate: int = 1411
    # Eclipse Music enrichment field (Apple Music / MusicKit linking)
    isrc: str | None = None
    streamURL: str | None = None
    audioModes: list[str] = Field(default_factory=lambda: ["STEREO"])
    atmos: bool = False

    model_config = {"extra": "allow"}

class SearchResponse(BaseModel):
    """Dual envelope search response satisfying BitChord ('tracks') and Eclipse ('results')."""
    tracks: list[TrackItem]
    results: list[TrackItem]

class ResolveIsrcResponse(BaseModel):
    """ISRC resolution response conforming to Eclipse Music addon specification."""
    trackId: str | None = None
    id: str | None = None

class ResolveItem(BaseModel):
    """Item descriptor returned by Eclipse Music /resolve endpoint."""
    id: str
    type: str = "track"
    title: str
    artist: str
    isrc: str | None = None

class ResolveResponse(BaseModel):
    """Response returned by Eclipse Music /resolve endpoint."""
    item: ResolveItem | None = None

class StreamResponse(BaseModel):
    """Stream URL and container specification for player engines."""
    url: str
    format: str = "flac"
    codec: str = "flac"
    container: str = "flac"
    manifest: str = "none"
    bitDepth: int = 16
    sampleRate: int = 44100
    bitrate: int = 1411
    encrypted: bool = False

    model_config = {"extra": "allow"}

