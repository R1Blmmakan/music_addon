import unittest
import re
from app.main import sanitize_search_query, merge_and_rank_tracks
from app.providers.tidal import score_tidal_candidate

class TestSearchDiagnostics(unittest.TestCase):
    def test_sanitize_query_handles_parenthesized_feat(self):
        """Sanitization must normalize (feat. Artist) and [feat. Artist]."""
        raw_queries = [
            ("Tick Tock (feat. 24kGoldn)", "Tick Tock 24kGoldn"),
            ("Tick Tock (feat 24kGoldn)", "Tick Tock 24kGoldn"),
            ("Tick Tock (ft. 24kGoldn)", "Tick Tock 24kGoldn"),
            ("Tick Tock [feat. 24kGoldn]", "Tick Tock 24kGoldn"),
            ("Tick Tock feat. 24kGoldn", "Tick Tock 24kGoldn"),
        ]
        for raw, expected in raw_queries:
            cleaned = sanitize_search_query(raw)
            self.assertEqual(cleaned, expected, f"Failed for {raw!r}: got {cleaned!r}")

    def test_farben_tak_tik_mix_ranking_over_unrelated_tidal_results(self):
        """When Tidal returns irrelevant 'Tik Tak' tracks and Deezer has 'Farben (TAK TIK Mix)',
        the search ranking must place Deezer's exact track at #1, not #16."""
        tidal_tracks = [
            {"id": f"td:{i}", "title": "Tik Tak (Extended Mix)", "artist": "Doctor MC's", "album": "Tik Tak", "duration": 210.0}
            for i in range(15)
        ]
        deezer_tracks = [
            {"id": "dz:2664511252", "title": "Farben (TAK TIK Mix)", "artist": "Orange Sector", "album": "Farben (TAK TIK Mix)", "duration": 199.0},
            {"id": "dz:615467782", "title": "Farben (Alarm Mix)", "artist": "Orange Sector", "album": "Farben", "duration": 234.0},
        ]
        
        results = merge_and_rank_tracks(
            tidal_tracks=tidal_tracks,
            deezer_tracks=deezer_tracks,
            query="Farben (TAK TIK Mix)",
            preferred_provider="tidal"
        )
        self.assertTrue(len(results) > 0)
        self.assertEqual(results[0]["id"], "dz:2664511252", f"Top track should be Farben (TAK TIK Mix), got {results[0]}")

    def test_tick_tock_filters_tribute_parody_tracks(self):
        """Parody/cover tracks like 'Devil Bandit, My Mabel, 99kGoldn' must be filtered or demoted
        so they do not cause conflicting album rejections in BitChord."""
        genuine = {
            "id": 150917151, "title": "Tick Tock (feat. 24kGoldn)", "artist": {"name": "Clean Bandit"},
            "artists": [{"name": "Clean Bandit"}, {"name": "Mabel"}, {"name": "24kGoldn"}],
            "album": {"title": "Tick Tock (feat. 24kGoldn)"}, "duration": 178, "popularity": 75
        }
        parody = {
            "id": 192357438, "title": "Tick Tock", "artist": {"name": "Devil Bandit"},
            "artists": [{"name": "Devil Bandit"}, {"name": "My Mabel"}, {"name": "99kGoldn"}],
            "album": {"title": "Hot Summer Hits 2021"}, "duration": 176, "popularity": 10
        }
        
        score_gen = score_tidal_candidate(genuine, "tick tock clean bandit")
        score_par = score_tidal_candidate(parody, "tick tock clean bandit")
        
        self.assertIsNotNone(score_gen, "Genuine track should be accepted")
        # Parody track must be dropped (None) or heavily outscored
        self.assertTrue(score_par is None or score_gen < score_par - 500, "Parody track should be filtered or heavily outscored")

if __name__ == "__main__":
    unittest.main()
