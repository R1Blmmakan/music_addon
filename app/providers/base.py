from abc import ABC, abstractmethod

class MusicProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier string."""
        pass

    @abstractmethod
    def is_configured(self) -> bool:
        """Return True if required tokens or endpoints are provided."""
        pass

    @abstractmethod
    async def search(self, query: str, limit: int = 5) -> list[dict]:
        """Search tracks and return list of BitChord-compatible track dictionaries."""
        pass

    @abstractmethod
    async def get_stream(self, track_id: str, quality: str = "lossless") -> dict | None:
        """Resolve track ID to stream metadata and URL."""
        pass

    @abstractmethod
    async def health(self) -> bool:
        """Perform health check against provider backend."""
        pass
