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

    def test_dash_delivers_native_dash_for_exoplayer_bitchord(self):
        """When client supports DASH (BitChord / ExoPlayer), Tidal DASH manifest must be delivered natively."""
        from app.main import _resolve_stream_inner, deezer, tidal
        import asyncio

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
                            "td:463900374", "lossless", "https://api.r1fikri.dev", needs_progressive=False
                        )
                        # ExoPlayer / BitChord must receive the native Tidal DASH stream
                        self.assertEqual(res["manifest"], "dash")
                        self.assertEqual(res["container"], "fmp4")
                        self.assertIn("/dash/td/", res["url"])

        asyncio.run(run_test())

    def test_dash_to_deezer_progressive_flac_fallback_for_web_and_apple(self):
        """When client requires progressive stream (Web / Apple AVPlayer), server delivers progressive Deezer FLAC."""
        from app.main import _resolve_stream_inner, deezer, tidal
        import asyncio

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
                            "td:463900374", "lossless", "https://api.r1fikri.dev", needs_progressive=True
                        )
                        # Web and Apple clients must receive progressive Deezer FLAC
                        self.assertEqual(res["manifest"], "none")
                        self.assertEqual(res["container"], "flac")
                        self.assertIn("/audio/dz/", res["url"])

        asyncio.run(run_test())

    def test_client_progressive_detection_rules(self):
        """Verify _needs_progressive_stream correctly differentiates BitChord/ExoPlayer from Eclipse/Web/Apple."""
        from app.main import _needs_progressive_stream
        from starlette.requests import Request

        def make_request(headers=None, query_string=""):
            scope = {
                "type": "http",
                "method": "GET",
                "path": "/stream/td:123",
                "headers": [(k.lower().encode("latin1"), v.encode("latin1")) for k, v in (headers or {}).items()],
                "query_string": query_string.encode("latin1"),
            }
            return Request(scope)

        # BitChord / ExoPlayer Android client -> DASH capable (needs_progressive=False)
        req_bitchord = make_request(headers={"User-Agent": "com.music.bitchord/1.0.0 (Android 14; Mobile)"})
        self.assertFalse(_needs_progressive_stream(req_bitchord))

        req_exoplayer = make_request(headers={"User-Agent": "AndroidXMedia3/1.11.0 (ExoPlayer)"})
        self.assertFalse(_needs_progressive_stream(req_exoplayer))

        req_okhttp = make_request(headers={"User-Agent": "okhttp/4.12.0"})
        self.assertFalse(_needs_progressive_stream(req_okhttp))

        # Apple AVPlayer / iOS -> needs progressive
        req_apple = make_request(headers={"User-Agent": "AppleCoreMedia/1.0.0.21F79 (iPhone; U; CPU OS 17_5 like Mac OS X)"})
        self.assertTrue(_needs_progressive_stream(req_apple))

        # Eclipse Music Web client (carrying Origin) -> needs progressive
        req_eclipse = make_request(headers={
            "Origin": "https://eclipsemusic.app",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36"
        })
        self.assertTrue(_needs_progressive_stream(req_eclipse))

        # Explicit query param overrides
        req_explicit_dash = make_request(query_string="prefer=dash", headers={"Origin": "https://eclipsemusic.app"})
        self.assertFalse(_needs_progressive_stream(req_explicit_dash))

        req_explicit_prog = make_request(query_string="prefer=progressive", headers={"User-Agent": "AndroidXMedia3/1.11.0"})
        self.assertTrue(_needs_progressive_stream(req_explicit_prog))

    def test_manifest_v191_flags_and_quality_settings(self):
        """Verify BitChord v1.9.1 flags and 3-tier quality options in manifest."""
        resp = self.client.get("/manifest.json")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        # BitChord v1.9.1 schema flags
        self.assertTrue(data.get("allowDownloads"))
        self.assertTrue(data.get("checkValidLossless"))
        self.assertFalse(data.get("bitchordAuth"))
        self.assertEqual(data.get("version"), "2.3.0")

        # Quality settings options: lossless, high, low
        quality_setting = next((s for s in data.get("settings", []) if s.get("key") == "quality"), None)
        self.assertIsNotNone(quality_setting)
        self.assertEqual(quality_setting.get("default"), "lossless")
        values = [opt["value"] for opt in quality_setting.get("options", [])]
        self.assertIn("lossless", values)
        self.assertIn("high", values)
        self.assertIn("low", values)

    def test_explicit_song_ranking_and_deduplication(self):
        """Explicit versions must rank higher than clean radio edits, and replace them during deduplication."""
        from app.main import score_track_relevance, merge_and_rank_tracks

        clean_track = {
            "id": "dz:111",
            "title": "São Paulo (Radio Edit)",
            "artist": "The Weeknd & Anitta",
            "explicit": False,
            "format": "flac"
        }
        explicit_track = {
            "id": "dz:222",
            "title": "São Paulo",
            "artist": "The Weeknd & Anitta",
            "explicit": True,
            "format": "flac"
        }

        # 1. Without 'clean' in query: explicit track must score higher
        score_clean = score_track_relevance(clean_track, "Sao Paulo The Weeknd Anitta", is_preferred=True)
        score_explicit = score_track_relevance(explicit_track, "Sao Paulo The Weeknd Anitta", is_preferred=True)
        self.assertGreater(score_explicit, score_clean)

        # 2. With 'clean' in query: clean track must score higher
        score_clean_q = score_track_relevance(clean_track, "Sao Paulo Clean The Weeknd", is_preferred=True)
        score_explicit_q = score_track_relevance(explicit_track, "Sao Paulo Clean The Weeknd", is_preferred=True)
        self.assertGreater(score_clean_q, score_explicit_q)

        # 3. Deduplication: when both clean and explicit tracks match the same title/artist, explicit track wins
        merged = merge_and_rank_tracks(
            tidal_tracks=[],
            deezer_tracks=[clean_track, explicit_track],
            query="Sao Paulo The Weeknd"
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["id"], "dz:222")
        self.assertTrue(merged[0]["explicit"])

    def test_quality_options_resolution(self):
        """Endpoint /stream/{item_id}?quality=... must respect requested quality tier."""
        from app.main import deezer, tidal
        import asyncio

        mock_high_stream = {
            "url": "https://api.r1fikri.dev/audio/dz/12345?quality=high",
            "format": "mp3",
            "codec": "mp3",
            "container": "mp3",
            "bitDepth": 16,
            "sampleRate": 44100,
            "bitrate": 320,
            "audioQuality": "HIGH",
        }
        mock_low_stream = {
            "url": "https://api.r1fikri.dev/audio/dz/12345?quality=low",
            "format": "mp3",
            "codec": "mp3",
            "container": "mp3",
            "bitDepth": 16,
            "sampleRate": 44100,
            "bitrate": 128,
            "audioQuality": "LOW",
        }

        with patch.object(deezer, "is_configured", return_value=True):
            with patch.object(deezer, "get_stream", side_effect=lambda tid, q: mock_high_stream if q == "high" else mock_low_stream):
                # Request High (320kbps)
                resp_high = self.client.get("/stream/dz:12345?quality=high")
                self.assertEqual(resp_high.status_code, 200)
                data_high = resp_high.json()
                self.assertEqual(data_high["audioQuality"], "HIGH")
                self.assertEqual(data_high["bitrate"], 320)
                self.assertIn("quality=high", data_high["url"])

                # Request Low (128kbps)
                resp_low = self.client.get("/stream/dz:12345?quality=low")
                self.assertEqual(resp_low.status_code, 200)
                data_low = resp_low.json()
                self.assertEqual(data_low["audioQuality"], "LOW")
                self.assertEqual(data_low["bitrate"], 128)
                self.assertIn("quality=low", data_low["url"])

    def test_tidal_cd_quality_lossless_handling(self):
        """When Tidal returns HIGH for HI_RES_LOSSLESS, _fetch_playback_info must continue to LOSSLESS and succeed."""
        from app.main import tidal
        import asyncio

        def mock_get(url, headers=None, params=None):
            resp = MagicMock()
            resp.status_code = 200
            q = params.get("audioquality")
            if q == "HI_RES_LOSSLESS":
                # Simulated Tidal downgrade to AAC HIGH
                resp.json.return_value = {
                    "audioQuality": "HIGH",
                    "manifestMimeType": "application/vnd.tidal.bts",
                    "manifest": "e30=",
                }
            elif q == "LOSSLESS":
                # Simulated Tidal CD Quality FLAC
                resp.json.return_value = {
                    "audioQuality": "LOSSLESS",
                    "bitDepth": 16,
                    "sampleRate": 44100,
                    "manifestMimeType": "application/vnd.tidal.bts",
                    "manifest": "e30=",
                }
            return resp

        async def run_test():
            with patch.object(tidal.client, "get", side_effect=mock_get):
                data = await tidal._fetch_playback_info("197645628", ["HI_RES_LOSSLESS", "LOSSLESS"])
                self.assertIsNotNone(data)
                # Must have resolved the LOSSLESS tier instead of stopping at HIGH
                self.assertEqual(data.get("audioQuality"), "LOSSLESS")

        asyncio.run(run_test())



