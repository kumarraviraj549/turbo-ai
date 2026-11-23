import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tubeforge import alignment, jobs, planner, render
from tubeforge.http import ProviderError


class WordAlignmentTests(unittest.TestCase):
    def test_hash_cache_reuses_alignment_only_for_identical_audio(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio_path = root / "voice.wav"
            audio_path.write_bytes(b"first audio")
            words = [{"word": "hello", "start": 0.1, "end": 0.4}]

            with patch.object(alignment, "transcribe", return_value=words) as transcribe:
                cached = alignment.align_project_audio(audio_path, root, force=True)
                self.assertEqual(cached, words)
                self.assertEqual(json.loads((root / "alignment.json").read_text(encoding="utf-8")), words)
                alignment.align_project_audio(audio_path, root)
                self.assertEqual(transcribe.call_count, 1)

                audio_path.write_bytes(b"changed audio")
                alignment.align_project_audio(audio_path, root)
                self.assertEqual(transcribe.call_count, 2)

    def test_provider_failure_uses_local_faster_whisper(self):
        words = [{"word": " offline", "start": 0.0, "end": 0.4}]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "voice.wav"
            path.write_bytes(b"test audio")
            with patch.object(alignment, "_groq_transcribe",
                              side_effect=ProviderError("groq", "rate limited", status=429)), \
                    patch.object(alignment, "_local_transcribe", return_value=words) as local:
                self.assertEqual(alignment.transcribe(path), words)
            local.assert_called_once()

    def test_large_audio_is_split_and_word_times_are_offset_without_overlap_duplicates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "large.wav"
            with source.open("wb") as file:
                file.truncate(alignment.MAX_GROQ_BYTES + 1)

            chunk_calls = []

            def create_chunk(command, **_kwargs):
                Path(command[-1]).write_bytes(b"pcm")
                return subprocess.CompletedProcess(command, 0, "", "")

            def transcribe_chunk(path, _language):
                chunk_calls.append(path.name)
                index = len(chunk_calls) - 1
                if index == 0:
                    return [{"word": "first", "start": 0.1, "end": 0.3}]
                if index == 1:
                    return [{"word": "overlap", "start": 0.5, "end": 0.7},
                            {"word": "second", "start": 2.0, "end": 2.2}]
                return [{"word": "third", "start": 2.0, "end": 2.2}]

            with patch.object(alignment, "_ffprobe_duration", return_value=1500.0), \
                    patch.object(alignment.subprocess, "run", side_effect=create_chunk), \
                    patch.object(alignment, "_groq_one", side_effect=transcribe_chunk):
                words = alignment._groq_transcribe(source, "en")

        self.assertEqual(len(chunk_calls), 3)
        self.assertEqual([word["word"] for word in words], ["first", "second", "third"])
        self.assertGreater(words[1]["start"], 596.0)
        self.assertGreater(words[2]["start"], words[1]["start"])

    def test_dynamic_beats_respect_hook_and_body_maximums(self):
        words = []
        for index in range(140):
            start = index * 0.45
            text = f"word{index}." if index % 8 == 7 else f"word{index}"
            words.append({"word": text, "start": start, "end": start + 0.25})

        beats = alignment.dynamic_beats(words)
        self.assertTrue(beats)
        for beat in beats:
            limit = 4.5 if beat["start"] < 30.0 else 7.0
            self.assertLessEqual(beat["end"] - beat["start"], limit)
            self.assertTrue(beat["text"])

        intervals = alignment.visual_beat_intervals(words, 60.0)
        self.assertEqual(intervals[0]["start"], 0.0)
        self.assertAlmostEqual(intervals[-1]["end"], 60.0)
        for interval in intervals:
            limit = 4.5 if interval["start"] < 30.0 else 7.0
            self.assertLessEqual(interval["end"] - interval["start"], limit)

    def test_kinetic_ass_uses_active_word_color_and_context_words(self):
        words = [
            {"word": f"word{i}", "start": i * 0.4, "end": i * 0.4 + 0.25}
            for i in range(5)
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            output = render.write_kinetic_ass(words, Path(temp_dir) / "captions.ass", 1080, 1920, "#FF6600")
            content = output.read_text(encoding="utf-8")

        self.assertIn("PlayResX: 1080", content)
        self.assertIn("PlayResY: 1920", content)
        self.assertIn("&H000066FF", content)
        self.assertIn(",326,", content)
        events = [line for line in content.splitlines() if line.startswith("Dialogue:")]
        self.assertEqual(len(events), len(words))
        for event in events:
            visible = event.split(",", 9)[-1]
            self.assertLessEqual(len(visible.split()), 4)
            self.assertGreaterEqual(len(visible.split()), 2)

    def test_hard_cut_assembly_supports_zero_length_transitions(self):
        with patch.object(render, "_run_ffmpeg", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            duration = render._xfade_group(
                [Path("first.mkv"), Path("second.mkv")], [3.0, 4.0], [("fade", 0.0)],
                Path("joined.mkv"), 30,
            )

        graph = run.call_args.args[0][run.call_args.args[0].index("-filter_complex") + 1]
        self.assertIn("concat=n=2:v=1:a=0", graph)
        self.assertEqual(duration, 7.0)

    def test_external_voiceover_alignment_updates_resumable_project_state(self):
        words = [
            {"word": f"word{index}.", "start": index * 0.45, "end": index * 0.45 + 0.2}
            for index in range(100)
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            voiceover = root / "external.wav"
            voiceover.write_bytes(b"external audio")
            project = jobs.Project({
                "id": "alignment-project",
                "idea": "Alignment project",
                "settings": {**jobs.DEFAULT_SETTINGS, "external_voiceover_path": str(voiceover),
                             "alignment_model": "base"},
                "plan": planner.CinematicPlan(
                    scenes=[planner.Scene(title="Scene", narration="Spoken words", duration=60)]
                ).model_dump(),
                "scene_state": {},
                "logs": [],
            })

            with patch.object(jobs, "STUDIO_ROOT", root / "projects"), \
                    patch.object(alignment, "align_project_audio", return_value=words) as align:
                result = jobs._align_project_voice(project)

            self.assertEqual(result, words)
            self.assertEqual(project.data["alignment_word_count"], len(words))
            self.assertTrue(project.data["visual_beats"])
            self.assertEqual(align.call_args.args[0], voiceover)
            self.assertEqual(align.call_args.kwargs["model_size"], "base")


if __name__ == "__main__":
    unittest.main()
