import unittest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from app.main import app
from app.config import settings
from app.schemas import ManifestResponse, SearchResponse, StreamResponse

class TestDualCompatibility(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_manifest_dual_compatibility(self):
        """Manifest must satisfy both BitChord (resources, settings) and Eclipse (types, contentType)."""
        resp = self.client.get("/manifest.json")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        
        # Pydantic schema validation
        validated = ManifestResponse.model_validate(data)
        
        # BitChord expectations
        self.assertIn("search", validated.resources)
        self.assertIn("stream", validated.resources)
        self.assertTrue(len(validated.settings) > 0)
        self.assertEqual(validated.settings[0].key, "quality")
        
        # Eclipse Music expectations
        self.assertEqual(validated.contentType, "music")
        self.assertEqual(validated.types, ["track", "album", "artist"])
        self.assertEqual(validated.name, "Homelab HiFi")
        self.assertTrue(bool(validated.description))

    def test_cors_preflight_allows_browser_clients(self):
        """OPTIONS preflight must succeed with CORS headers for Eclipse Web clients."""
        resp = self.client.options(
            "/manifest.json",
            headers={
                "Origin": "https://eclipsemusic.app",
                "Access-Control-Request-Method": "GET"
            }
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("access-control-allow-origin"), "*")
        self.assertIn("GET", resp.headers.get("access-control-allow-methods", ""))

    def test_search_dual_envelope_format(self):
        """Search endpoint must return both 'tracks' (BitChord) and 'results' (Eclipse)."""
        resp = self.client.get("/search?q=farben")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        
        # Validate schema
        validated = SearchResponse.model_validate(data)
        self.assertIn("tracks", data)
        self.assertIn("results", data)
        self.assertEqual(len(validated.tracks), len(validated.results))
        
        if validated.tracks:
            first_track = validated.tracks[0]
            # Ensure essential BitChord fields exist
            self.assertTrue(bool(first_track.id))
            self.assertTrue(bool(first_track.title))
            self.assertTrue(bool(first_track.format))
            # Ensure Eclipse metadata enrichment field exists in model
            self.assertTrue(hasattr(first_track, "isrc"))

    def test_auth_supports_bitchord_and_eclipse_formats(self):
        """When an ACCESS_TOKEN is set, all supported token formats must be accepted."""
        test_token = "secret12345"
        with patch.object(settings, "access_token", test_token):
            # 1. Unauthenticated request should be rejected
            unauth = self.client.get("/manifest.json")
            self.assertEqual(unauth.status_code, 401)
            
            # 2. BitChord path-prefix format: /{token}/manifest.json
            bitchord_path = self.client.get(f"/{test_token}/manifest.json")
            self.assertEqual(bitchord_path.status_code, 200)
            self.assertEqual(bitchord_path.json()["name"], "Homelab HiFi")
            
            # 3. Eclipse Music query parameter format: /manifest.json?token={token}
            eclipse_query = self.client.get(f"/manifest.json?token={test_token}")
            self.assertEqual(eclipse_query.status_code, 200)
            
            # 4. Standard Bearer Authorization header:
            bearer_hdr = self.client.get("/manifest.json", headers={"Authorization": f"Bearer {test_token}"})
            self.assertEqual(bearer_hdr.status_code, 200)
            
            # 5. X-Access-Token header:
            custom_hdr = self.client.get("/manifest.json", headers={"X-Access-Token": test_token})
            self.assertEqual(custom_hdr.status_code, 200)
            
            # 6. CORS OPTIONS preflight must remain open without token
            options_req = self.client.options(
                "/manifest.json",
                headers={"Origin": "https://eclipsemusic.app", "Access-Control-Request-Method": "GET"}
            )
            self.assertEqual(options_req.status_code, 200)

    def test_resolve_isrc_endpoint(self):
        """Test GET /resolve-isrc returns trackId conforming to Eclipse specification."""
        from app.main import deezer
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": 3579685431, "title": "The Fate of Ophelia"}

        with patch.object(deezer, "is_configured", return_value=True):
            with patch.object(deezer.client, "get", return_value=mock_resp):
                resp = self.client.get("/resolve-isrc?isrc=USUG12506436")
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertIn("trackId", data)
                self.assertIn("id", data)
                self.assertEqual(data["trackId"], "dz:3579685431")

        # Test with non-existent / empty ISRC
        resp_404 = self.client.get("/resolve-isrc?isrc=")
        self.assertEqual(resp_404.status_code, 404)

    def test_resolve_endpoint(self):
        """Test GET /resolve returns item dict for queue generation and autoplay."""
        from app.main import deezer
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": 3579685431,
            "title": "The Fate of Ophelia",
            "artist": {"name": "Taylor Swift"}
        }

        with patch.object(deezer, "is_configured", return_value=True):
            with patch.object(deezer.client, "get", return_value=mock_resp):
                resp = self.client.get("/resolve?title=The+Fate+of+Ophelia&artist=Taylor+Swift&isrc=USUG12506436")
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertIn("item", data)
                self.assertIsNotNone(data["item"])
                self.assertEqual(data["item"]["type"], "track")
                self.assertEqual(data["item"]["id"], "dz:3579685431")

    def test_track_duration_is_integer(self):
        """Test that track duration is serialized as integer to prevent Dart int.tryParse() returning null."""
        from app.main import deezer
        mock_tracks = [{
            "id": "dz:123456",
            "title": "Test Song",
            "artist": "Test Artist",
            "album": "Test Album",
            "duration": 226,
            "artworkURL": "https://example.com/art.jpg",
            "format": "flac",
            "audioQuality": "LOSSLESS",
            "bitrate": 1411,
            "isrc": "US1234567890",
        }]
        with patch.object(deezer, "is_configured", return_value=True):
            with patch.object(deezer, "search", return_value=mock_tracks):
                resp = self.client.get("/search?q=Test+Song")
                self.assertEqual(resp.status_code, 200)
                tracks = resp.json().get("tracks", [])
                if tracks:
                    first = tracks[0]
                    self.assertIsInstance(first["duration"], int)
                    self.assertEqual(first["duration"], 226)

    def test_dash_to_deezer_progressive_flac_fallback(self):
        """When Tidal returns a DASH manifest, the server must deliver progressive Deezer FLAC."""
        from app.main import _resolve_stream_inner, deezer, tidal
        import asyncio

        # Mock tidal.get_stream returning DASH manifest
        mock_dash_res = {
            "url": "https://api.r1fikri.dev/dash/td/463900374.mpd",
            "format": "flac",
            "codec": "flac",
            "container": "fmp4",
            "manifest": "dash",
            "bitDepth": 24,
            "sampleRate": 48000,
            "bitrate": 1738,
            "encrypted": False
        }
        mock_dz_res = {
            "url": "https://api.r1fikri.dev/audio/dz/3579685431",
            "format": "flac",
            "codec": "flac",
            "container": "flac",
            "manifest": "none",
            "bitDepth": 16,
            "sampleRate": 44100,
            "bitrate": 1411,
            "encrypted": False
        }

        async def run_test():
            with patch.object(deezer, "is_configured", return_value=True):
                with patch.object(tidal, "get_stream", return_value=mock_dash_res):
                    with patch("app.main._tidal_to_deezer_fallback", return_value=mock_dz_res):
                        res = await _resolve_stream_inner(
                            "td:463900374", "lossless", "https://api.r1fikri.dev", is_apple_client=False
                        )
                        # Even with is_apple_client=False, progressive FLAC should be delivered!
                        self.assertEqual(res["manifest"], "none")
                        self.assertEqual(res["container"], "flac")
                        self.assertIn("/audio/dz/", res["url"])

        asyncio.run(run_test())

