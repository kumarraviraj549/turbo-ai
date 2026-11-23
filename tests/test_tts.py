import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from tubeforge import audio, cache, jobs, planner, tts
from tubeforge.http import ProviderError


class ProductionTTSTests(unittest.TestCase):
    def test_splitter_respects_character_limit_and_paragraph_boundaries(self):
        text = ("Sentence. " * 35) + "\n\n" + ("Another sentence. " * 20)
        chunks = tts.split_text_chunks(text, 100)

        self.assertTrue(chunks)
        self.assertTrue(all(0 < len(chunk.text) <= 100 for chunk in chunks))
        self.assertTrue(any(chunk.paragraph_start for chunk in chunks))
        self.assertFalse(chunks[0].paragraph_start)

    def test_stitcher_crossfades_chunks_and_keeps_paragraph_pause(self):
        chunks = [
            (tts.SpeechChunk("first words", False), np.ones(2400, dtype=np.float32) * 0.1),
            (tts.SpeechChunk("more words", False), np.ones(2400, dtype=np.float32) * 0.1),
            (tts.SpeechChunk("new paragraph", True), np.ones(2400, dtype=np.float32) * 0.1),
        ]
        samples, words = tts._stitch_chunks(chunks)

        self.assertEqual(len(samples), 2400 + 2400 - 720 + 10800 + 2400)
        self.assertTrue(any(word.start >= (len(samples) - 2400) / 24000 for word in words))
        self.assertTrue(np.all(np.isfinite(samples)))

    def test_interrupted_kokoro_generation_reuses_completed_chunks(self):
        text = " ".join(f"Sentence number {index}." for index in range(85))
        chunks = tts.split_text_chunks(text, 220)
        self.assertGreaterEqual(len(chunks), 3)
        generated = []

        def fake_generate(chunk_text, voice, speed):
            generated.append(chunk_text)
            if len(generated) == 2:
                raise tts.TTSError("simulated interrupted chunk")
            return np.ones(2400, dtype=np.float32) * 0.1

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(tts, "_kokoro_cache_dir", return_value=Path(temp_dir)), \
                    patch.object(tts, "_kokoro_generate_chunk", side_effect=fake_generate):
                with self.assertRaises(tts.TTSError):
                    tts._synthesize_kokoro(text, "af_bella", 1.0)
                samples, words = tts._synthesize_kokoro(text, "af_bella", 1.0)

        self.assertEqual(len(generated), len(chunks) + 1)
        self.assertEqual(generated[0], chunks[0].text)
        self.assertEqual(generated[1], chunks[1].text)
        self.assertEqual(generated[2], chunks[1].text)
        self.assertGreater(len(samples), 0)
        self.assertTrue(words)

    def test_groq_rate_limit_falls_back_to_local_kokoro(self):
        samples = np.ones(24000, dtype=np.float32) * 0.1
        out = Path(tempfile.gettempdir()) / "tubeforge-kokoro-fallback-test.mp3"
        with patch.object(tts, "_synthesize_remote_chunks",
                          side_effect=ProviderError("groq_tts", "rate limited", status=429)), \
                patch.object(tts, "_synthesize_kokoro", return_value=(samples, [tts.Word(0, 1, "hello")])):
            result = tts.synthesize("Hello world.", out, engine="groq_orpheus", voice="troy")

        self.assertEqual(result.engine, "kokoro")
        self.assertEqual(result.voice, "af_bella")
        self.assertTrue(result.warnings)
        result.path.unlink(missing_ok=True)

    def test_groq_provider_request_errors_fall_back_to_local_kokoro(self):
        samples = np.ones(24000, dtype=np.float32) * 0.1
        out = Path(tempfile.gettempdir()) / "tubeforge-kokoro-provider-error-test.mp3"
        with patch.object(tts, "_synthesize_remote_chunks",
                          side_effect=ProviderError("groq_tts", "model unavailable", status=400)), \
                patch.object(tts, "_synthesize_kokoro", return_value=(samples, [tts.Word(0, 1, "hello")])):
            result = tts.synthesize("Hello world.", out, engine="groq_orpheus", voice="troy")

        self.assertEqual(result.engine, "kokoro")
        self.assertTrue(any("HTTP 400" in warning for warning in result.warnings))
        result.path.unlink(missing_ok=True)

    def test_groq_daily_request_limit_is_tracked(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(cache, "USAGE_FILE", root / "usage.json"), \
                    patch.object(cache, "OUTPUT_ROOT", root):
                for _ in range(100):
                    self.assertTrue(cache.reserve_daily_calls("groq_tts", 100))
                self.assertFalse(cache.reserve_daily_calls("groq_tts", 100))
                self.assertEqual(cache.usage_today()["calls"]["groq_tts"], 100)

    def test_final_mix_uses_voice_cleanup_ducking_and_ebu_normalization(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sr = audio.SR
            t = np.arange(sr * 2) / sr
            voice = np.stack([np.sin(2 * np.pi * 220 * t), np.sin(2 * np.pi * 220 * t)], axis=1).astype(np.float32)
            music = np.stack([np.sin(2 * np.pi * 55 * t) * 0.1,
                              np.sin(2 * np.pi * 55 * t) * 0.1], axis=1).astype(np.float32)
            voice_path, music_path = root / "voice.wav", root / "music.wav"
            audio._write_pcm_wav(voice_path, voice)
            audio._write_pcm_wav(music_path, music)
            master = root / "master.m4a"
            result = audio.mix_final(
                voice_path, music_path, None, master, 2.0,
                voice_vol=1.0, music_vol=0.35, fx_vol=0.5, duck=0.6,
                loudness=-14.0, duck_db=-24.0,
            )
            self.assertTrue(result.is_file())
            self.assertGreater(result.stat().st_size, 100)

    def test_imported_voiceover_skips_provider_synthesis_and_duplicates_with_project(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            voiceover = root / "external.wav"
            voiceover.write_bytes(b"external audio")
            project = jobs.Project({
                "id": "external-voice-test",
                "idea": "External voice test",
                "settings": {**jobs.DEFAULT_SETTINGS, "external_voiceover_path": str(voiceover)},
                "scene_state": {},
                "logs": [],
            })
            scene = planner.Scene(title="Scene", narration="Narration")

            with patch.object(tts, "synthesize") as synthesize:
                self.assertEqual(jobs.ensure_voice(project, scene, project.log), [])
            synthesize.assert_not_called()

            with patch.object(jobs, "STUDIO_ROOT", root / "projects"):
                project.save()
                duplicate = jobs.duplicate_project(project.id)
                duplicate_voiceover = Path(duplicate.settings["external_voiceover_path"])
                self.assertTrue(duplicate_voiceover.exists())
                self.assertEqual(duplicate_voiceover.read_bytes(), voiceover.read_bytes())
                jobs.delete_project(project.id)
                jobs.delete_project(duplicate.id)


if __name__ == "__main__":
    unittest.main()
