import subprocess
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tubeforge import jobs, planner, render


class VideoDecodabilityTests(unittest.TestCase):
    @patch("tubeforge.render._run_ffmpeg")
    def test_decodes_entire_video_and_fails_on_decode_errors(self, run_ffmpeg):
        run_ffmpeg.return_value = subprocess.CompletedProcess([], 1, "", "Invalid NAL unit size")

        self.assertFalse(render.video_is_decodable(Path("scene.mp4")))
        command = run_ffmpeg.call_args.args[0]
        self.assertIn("-xerror", command)
        self.assertEqual(command[-3:], ["-f", "null", "-"])

    @patch("tubeforge.render._run_ffmpeg")
    def test_valid_video_is_decodable(self, run_ffmpeg):
        run_ffmpeg.return_value = subprocess.CompletedProcess([], 0, "", "")

        self.assertTrue(render.video_is_decodable(Path("scene.mp4")))

    def test_corrupt_cached_beat_segments_are_regenerated(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "scene.mp4"
            source.write_bytes(b"source")
            output_dir = root / "beats"

            def fake_run(command, **_kwargs):
                if command[0] == "ffprobe":
                    return subprocess.CompletedProcess(command, 0, "2.0\n", "")
                Path(command[-1]).write_bytes(b"encoded beat")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch.object(render.subprocess, "run", side_effect=fake_run), \
                    patch.object(render, "video_is_decodable", return_value=True):
                original = render.split_video_clip(source, [0.0, 1.0, 2.0], output_dir, 30)

            original[0].write_bytes(b"corrupt")
            with patch.object(render.subprocess, "run", side_effect=fake_run), \
                    patch.object(render, "video_is_decodable", side_effect=[False, True, True]) as decode:
                rebuilt = render.split_video_clip(source, [0.0, 1.0, 2.0], output_dir, 30)
                rebuilt_bytes = rebuilt[0].read_bytes()

        self.assertEqual(rebuilt, original)
        self.assertEqual(decode.call_count, 3)
        self.assertEqual(rebuilt_bytes, b"encoded beat")

    def test_timed_out_beat_encode_cleans_partial_output_and_reports_render_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "scene.mp4"
            source.write_bytes(b"source")
            output_dir = root / "beats"

            def fake_run(command, **kwargs):
                if command[0] == "ffprobe":
                    return subprocess.CompletedProcess(command, 0, "2.0\n", "")
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])

            with patch.object(render.subprocess, "run", side_effect=fake_run):
                with self.assertRaisesRegex(render.RenderError, "timed out after 600 seconds"):
                    render.split_video_clip(source, [0.0, 1.0, 2.0], output_dir, 30)

            self.assertFalse((output_dir / "scene_beat_000.part.mkv").exists())

    def test_assembly_without_kinetic_captions_exports_with_no_ass_file(self):
        class FakeTrack:
            def __init__(self, _path, _seconds):
                pass

            def add(self, *_args, **_kwargs):
                pass

            def add_loop(self, *_args, **_kwargs):
                pass

            def write(self, path):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"audio")
                return path

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            clip = root / "scene.mp4"
            image = root / "scene.jpg"
            clip.write_bytes(b"scene")
            image.write_bytes(b"image")
            scene = planner.Scene(id="scene-1", title="Scene", duration=2.0)
            project = jobs.Project({
                "id": "assembly-test",
                "settings": {**jobs.DEFAULT_SETTINGS, "end_card": False, "music_mood": "none",
                             "shorts_teaser": False, "kinetic_captions": False, "thumb_variants": False},
                "plan": planner.CinematicPlan(title="Test Film", scenes=[scene]).model_dump(by_alias=True),
                "scene_state": {"scene-1": {"status": "done", "clip": str(clip), "clip_len": 2.0,
                                               "image": str(image)}},
                "job": {"status": "running", "progress": 0.0, "stage": ""},
                "logs": [],
            })

            def write_output(*args, **kwargs):
                output = Path(kwargs.get("out") or args[2])
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"output")
                return output

            def write_sidecar(_chunks, output):
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text("", encoding="utf-8")
                return output

            def write_assembly(_clips, _durations, _transitions, output, *_args, **_kwargs):
                Path(output).parent.mkdir(parents=True, exist_ok=True)
                Path(output).write_bytes(b"assembled")

            with patch.object(jobs, "STUDIO_ROOT", root / "projects"), \
                    patch.object(jobs.audio, "Track", FakeTrack), \
                    patch.object(jobs.audio, "mix_final", side_effect=lambda *args, **kwargs: root / "mix.m4a"), \
                    patch.object(jobs.render, "assemble_video", side_effect=write_assembly), \
                    patch.object(jobs.render, "export_final", side_effect=write_output) as export_final, \
                    patch.object(jobs.render, "write_srt", side_effect=write_sidecar), \
                    patch.object(jobs.render, "write_vtt", side_effect=write_sidecar), \
                    patch.object(jobs.render, "make_thumbnail", side_effect=write_output), \
                    patch.object(jobs.render, "write_kinetic_ass") as write_ass:
                jobs.assemble_film(project, threading.Event())

        self.assertIsNone(export_final.call_args.kwargs["subtitle_ass"])
        write_ass.assert_not_called()

    def test_multi_group_assembly_finalizes_with_stream_copy_not_path_replace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            clips = [root / "first.mp4", root / "second.mp4"]
            output = root / "assembled.mkv"

            def finish_copy(command, **_kwargs):
                Path(command[-1]).write_bytes(b"final video")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch.object(render, "_xfade_group", return_value=5.0), \
                    patch.object(render.subprocess, "run", side_effect=finish_copy) as run_ffmpeg, \
                    patch.object(Path, "replace", side_effect=AssertionError("Path.replace must not handle media")):
                duration = render.assemble_video(
                    clips, [3.0, 3.0], [("fade", 1.0)], output, 30, root / "work",
                )

            self.assertEqual(duration, 5.0)
            command = run_ffmpeg.call_args.args[0]
            self.assertIn("-c", command)
            self.assertIn("copy", command)
            self.assertIn("-nostdin", command)


if __name__ == "__main__":
    unittest.main()
