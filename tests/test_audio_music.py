import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tubeforge import audio, jobs


class AudioMusicTests(unittest.TestCase):
    def test_ambient_scores_vary_by_seed_but_remain_slow(self):
        first = audio._ambient_variation(100)
        second = audio._ambient_variation(101)

        self.assertEqual(first, audio._ambient_variation(100))
        self.assertNotEqual(first[:3], second[:3])
        self.assertTrue(52 <= first[1] < 70)
        self.assertTrue(52 <= second[1] < 70)

    def test_music_seed_is_stable_per_project_and_distinct_between_projects(self):
        self.assertEqual(jobs._music_seed("project-one"), jobs._music_seed("project-one"))
        self.assertNotEqual(jobs._music_seed("project-one"), jobs._music_seed("project-two"))
        self.assertNotEqual(jobs._music_seed("project-one"), jobs._music_seed("project-one", 1))

    def test_cinematic_mood_arrangements_are_distinct_and_repeatable_by_seed(self):
        for mood in audio.MOODS:
            if mood == "ambient":
                continue
            first = audio._mood_variation(mood, 100)
            self.assertEqual(first, audio._mood_variation(mood, 100))
            second = audio._mood_variation(mood, 101)
            self.assertNotEqual(
                (first["root"], first["bpm"], first["prog"]),
                (second["root"], second["bpm"], second["prog"]),
            )
            self.assertTrue(40 <= first["bpm"] <= 140)

    def test_composed_score_changes_between_project_seeds(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(audio, "AUDIO_CACHE", Path(directory)):
                first_harmony, _, _ = audio.compose_score("epic", 1001)
                second_harmony, _, _ = audio.compose_score("epic", 1002)
                first_digest = hashlib.sha256(first_harmony.read_bytes()).digest()
                second_digest = hashlib.sha256(second_harmony.read_bytes()).digest()
        self.assertNotEqual(first_digest, second_digest)


if __name__ == "__main__":
    unittest.main()
