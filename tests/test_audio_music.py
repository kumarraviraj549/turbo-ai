import unittest

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


if __name__ == "__main__":
    unittest.main()
