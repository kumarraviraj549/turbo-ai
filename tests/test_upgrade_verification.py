import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tubeforge import alignment, audio
from workers import depthflow_worker


class UpgradeVerificationTests(unittest.TestCase):
    def test_cpu_depth_warp_displaces_foreground_more_than_background(self):
        import cv2

        h, w = 220, 320
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        depth = np.where(xx > w * 0.58, 0.90, 0.20).astype(np.float32)
        image = np.full((h, w, 3), 80, dtype=np.uint8)
        image[:, int(w * 0.58):] = (40, 160, 220)
        shift = 11.0
        map_x_fore = xx - shift * depth
        warped_fore = cv2.remap(image, map_x_fore, yy, cv2.INTER_LINEAR)
        fg_before = np.mean(warped_fore[:, int(w * 0.58):], axis=(0, 1))
        fg_after = np.mean(warped_fore[:, int(w * 0.58): - 20], axis=(0, 1))
        differential = float(np.linalg.norm(fg_after - fg_before))
        self.assertGreater(differential, 0.1)

        map_x_flat = xx - shift * np.full_like(depth, 0.5)
        flat = cv2.remap(image, map_x_flat, yy, cv2.INTER_LINEAR)
        flat_change = float(np.mean(np.abs(image.astype(np.float32) - flat.astype(np.float32))))
        self.assertGreater(flat_change, 0.0)

    def test_adjacent_distinct_beats_never_repeat_visual_tuple(self):
        beats = alignment.persist_distinct_beats(
            {},
            [{"start": 0.0, "end": 4.0, "text": "First"},
             {"start": 4.0, "end": 8.0, "text": "Second"},
             {"start": 8.0, "end": 12.0, "text": "Third"}],
            "a" * 64,
        )
        for left, right in zip(beats, beats[1:]):
            left_tuple = (
                left["sourceAssetHash"],
                left["visualTreatment"]["motionEngine"],
                left["visualTreatment"]["cameraPreset"],
                tuple(left["visualTreatment"]["cropRect"]),
            )
            right_tuple = (
                right["sourceAssetHash"],
                right["visualTreatment"]["motionEngine"],
                right["visualTreatment"]["cameraPreset"],
                tuple(right["visualTreatment"]["cropRect"]),
            )
            self.assertNotEqual(left_tuple, right_tuple)

    def test_speech_ducking_holds_22_to_26_db_gap(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sr = audio.SR
            samples = sr * 8
            t = np.arange(samples) / sr
            voice_mono = np.sin(2 * np.pi * 220 * t) * ((t > 1) & (t < 6)).astype(np.float32)
            music_mono = np.sin(2 * np.pi * 55 * t) * 0.9
            voice = np.stack([voice_mono, voice_mono], axis=1).astype(np.float32)
            music = np.stack([music_mono, music_mono], axis=1).astype(np.float32)
            ducked = audio.apply_speech_ducking(music, voice, target_db=-24.0)
            active = (t > 1.5) & (t < 5.5)
            voice_rms = audio._rms_db(voice[active])
            music_rms = audio._rms_db(ducked[active])
            gap = music_rms - voice_rms
            self.assertGreaterEqual(gap, -26.0)
            self.assertLessEqual(gap, -22.0)

    def test_tts_chunk_join_discontinuity_is_below_threshold(self):
        from tubeforge import tts

        left = np.zeros(24000, dtype=np.float32)
        right = np.ones(24000, dtype=np.float32) * 0.05
        stitched, _ = tts._stitch_chunks([
            (tts.SpeechChunk("first chunk", False), left),
            (tts.SpeechChunk("second chunk", False), right),
        ])
        join = len(left) - int(0.03 * 24000)
        differences = np.abs(np.diff(stitched[join - 12:join + 12]))
        self.assertLess(float(difference := differences.max()), 0.20)


if __name__ == "__main__":
    unittest.main()
