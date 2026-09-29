import unittest
import asyncio
import re
from app.main import _clean_title_for_search, sanitize_search_query
from app.providers.deezer import DeezerProvider

class TestVersionAndMixResolution(unittest.TestCase):
    def test_query_wants_version_detection(self):
        """Ensure version, mix, remix, live, feat are recognized as version queries."""
        version_keywords = ("remix", "mix", "dub", "edit", "version", "acoustic", "instrumental", "live", "feat", "ft")
        
        want_version_queries = [
            "farben mix orange sector",
            "what it is version doechii",
            "what it is solo version",
            "tick tock clean bandit feat 24kgoldn",
            "hotel california acoustic",
            "numb encore live",
            "stay remix kid laroi",
            "radioactive club mix",
        ]
        for q in want_version_queries:
            q_lower = q.lower()
            detected = any(re.search(r'\b' + re.escape(k) + r'\b', q_lower) for k in version_keywords)
            self.assertTrue(detected, f"Query '{q}' should be detected as wanting a version")

        clean_queries = [
            "livin on a prayer bon jovi",
            "shape of you ed sheeran",
            "blinding lights the weeknd",
            "paint it black rolling stones",
        ]
        for q in clean_queries:
            q_lower = q.lower()
            detected = any(re.search(r'\b' + re.escape(k) + r'\b', q_lower) for k in version_keywords)
            self.assertFalse(detected, f"Query '{q}' should NOT be detected as wanting a version")

    def test_clean_title_preserves_version_when_needed(self):
        """Fallback search must preserve mix/version details when searching fallback provider."""
        title = "What It Is"
        version = "Solo Version"
        full = f"{title} ({version})"
        
        cleaned_preserve = _clean_title_for_search(full, preserve_version=True)
        self.assertIn("solo version", cleaned_preserve.lower())

        cleaned_strip = _clean_title_for_search(full, preserve_version=False)
        self.assertNotIn("solo version", cleaned_strip.lower())
        self.assertEqual(cleaned_strip.lower(), "what it is")

        mix_title = "Farben (Alarm Mix)"
        self.assertIn("alarm mix", _clean_title_for_search(mix_title, preserve_version=True).lower())
        self.assertEqual(_clean_title_for_search(mix_title, preserve_version=False).lower(), "farben")

    def test_deezer_search_ranks_mix_first_when_requested(self):
        """Test Deezer search directly to confirm mixes are #1 when user asks for a mix."""
        async def run():
            dp = DeezerProvider(arl="dummy", public_host="")
            results = await dp.search("farben mix orange sector", limit=3)
            self.assertTrue(len(results) > 0)
            top_title = results[0]["title"].lower()
            self.assertIn("mix", top_title)
            self.assertIn("alarm mix", top_title)

        asyncio.run(run())

    def test_deezer_search_prefers_canonical_when_plain_requested(self):
        """Test Deezer search directly to confirm clean query demotes remixes."""
        async def run():
            dp = DeezerProvider(arl="dummy", public_host="")
            results = await dp.search("livin on a prayer bon jovi", limit=3)
            self.assertTrue(len(results) > 0)
            top_title = results[0]["title"].lower()
            self.assertNotIn("remix", top_title)
            self.assertNotIn("live", top_title)
            self.assertNotIn("karaoke", top_title)

        asyncio.run(run())

    def test_deezer_search_solo_version_when_requested(self):
        """Test Deezer search returns Solo Version when user explicitly queries it."""
        async def run():
            dp = DeezerProvider(arl="dummy", public_host="")
            results = await dp.search("What It Is Solo Version Doechii", limit=3)
            self.assertTrue(len(results) > 0)
            top_title = results[0]["title"].lower()
            self.assertIn("solo version", top_title)

        asyncio.run(run())

if __name__ == "__main__":
    unittest.main()
