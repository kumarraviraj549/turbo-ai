import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tubeforge import alignment, config, jobs, planner


class PartTwoFoundationTests(unittest.TestCase):
    def test_channel_library_round_trips_and_series_log_keeps_all_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "output" / "cinematic"
            with patch.object(jobs, "STUDIO_ROOT", root):
                library = {"visualBible": {"stylePrefix": "original style"},
                           "brandPalette": {"brand_accent_color": "#123456"},
                           "captionPreset": "Shorts Pop",
                           "narratorVoice": {"engine": "kokoro", "voice": "af_bella"}}
                jobs.save_channel_library("Example Channel", library)
                self.assertEqual(jobs.load_channel_library("Example Channel")["visualBible"],
                                 library["visualBible"])
                plan = planner.CinematicPlan(title="Episode", logline="Topic", hook="Opening line")
                for index in range(30):
                    jobs.append_series_history("Example Channel", f"project-{index}", plan)
                jobs.append_series_history("Example Channel", "project-29", plan)
                history_path = jobs.channel_dir("Example Channel") / "series_history.json"
                stored = json.loads(history_path.read_text(encoding="utf-8"))["videos"]
                self.assertEqual(len(stored), 30)
                self.assertEqual(len(jobs.load_series_history("Example Channel")), 25)

    def test_private_article_urls_are_rejected_without_fetching(self):
        for url in ("http://127.0.0.1/article", "http://localhost/article", "file:///etc/passwd"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                jobs.fetch_article_text(url)

    def test_alignment_chapters_start_at_zero_and_have_three_short_film_sections(self):
        words = [{"word": f"word{index}.", "start": float(index), "end": float(index) + 0.4}
                 for index in range(60)]
        chapters = jobs._alignment_chapters(words, 60.0)
        self.assertEqual(len(chapters), 3)
        self.assertEqual(chapters[0][0], 0.0)
        self.assertGreaterEqual(chapters[1][0], 10.0)
        self.assertGreaterEqual(chapters[2][0] - chapters[1][0], 10.0)

    def test_viral_windows_are_word_bounded_and_ranked(self):
        words = []
        for index in range(220):
            word = "revealed" if index % 20 == 0 else "story"
            words.append({"word": word, "start": index * 0.32, "end": index * 0.32 + 0.2})
        moments = alignment.score_viral_moments(words, 70.0, limit=3)
        self.assertTrue(1 <= len(moments) <= 3)
        self.assertTrue(all(30 <= item["duration"] <= 55 for item in moments))
        self.assertTrue(any(item["duration"] > 45 for item in moments))
        self.assertTrue(all(item["score"] <= 100 for item in moments))

    def test_flux_schnell_budget_estimate_matches_its_fixed_output_size(self):
        from tubeforge.images import estimate_neurons

        self.assertGreaterEqual(
            estimate_neurons("cf_flux1_schnell", 512, 512, steps=4),
            estimate_neurons("cf_flux1_schnell", 1024, 1024, steps=4),
        )

    def test_network_exposure_requires_password_but_local_defaults_do_not(self):
        self.assertFalse(config.network_binding_requires_password(False, ""))
        self.assertFalse(config.network_binding_requires_password(False, "127.0.0.1"))
        self.assertTrue(config.network_binding_requires_password(False, "0.0.0.0"))
        self.assertTrue(config.network_binding_requires_password(True, "localhost"))


if __name__ == "__main__":
    unittest.main()
