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
