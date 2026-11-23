import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from tubeforge import jobs, planner, render
from workers import cutout2d_worker, depthflow_worker


class PhaseFourTests(unittest.TestCase):
    def test_video_source_alias_round_trips_and_legacy_scenes_keep_default_engine(self):
        scene = planner.Scene(id="new", videoSource="depthflow_3d")
        serialized = scene.model_dump(by_alias=True)
        restored = planner.Scene(**serialized)
        legacy = planner.Scene(id="legacy")

        self.assertEqual(serialized["videoSource"], "depthflow_3d")
        self.assertEqual(restored.video_source, "depthflow_3d")
        self.assertEqual(legacy.video_source, "original_motion")

    def test_auto_choreographer_never_repeats_adjacent_presets(self):
        plan = planner.CinematicPlan(scenes=[planner.Scene(id=str(index)) for index in range(14)])

        jobs.auto_choreograph(plan)

        presets = [scene.motion_preset for scene in plan.scenes]
        camera_moves = [scene.camera_move for scene in plan.scenes]
        self.assertTrue(all(left != right for left, right in zip(presets, presets[1:])))
        self.assertTrue(set(presets).issubset(jobs.MOTION_PRESETS))
        self.assertEqual(camera_moves, [jobs.MOTION_CAMERA_MOVES[preset] for preset in presets])

    def test_only_opted_in_motion_engines_change_scene_cache_fingerprint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image = Path(temp_dir) / "scene.jpg"
            Image.new("RGB", (64, 36), "navy").save(image)
            spec = render.SceneRenderSpec(
                out_path=Path(temp_dir) / "clip.mp4",
                width=64, height=36, fps=12, duration=2.0,
                source_type="image", source_path=image,
            )

            baseline = jobs.spec_fingerprint(spec)
            changed_unused_settings = jobs.spec_fingerprint(
                replace(spec, motion_preset="circle", depth_intensity="cinematic", depth_dof=0.8)
            )
            depthflow = jobs.spec_fingerprint(replace(spec, video_source="depthflow_3d"))
            cutout = jobs.spec_fingerprint(replace(spec, video_source="cutout_2d"))

        self.assertEqual(baseline, changed_unused_settings)
        self.assertNotEqual(baseline, depthflow)
        self.assertNotEqual(depthflow, cutout)

    def test_depthflow_worker_failure_uses_builtin_renderer_and_reports_warning(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.png"
            Image.new("RGB", (64, 36), "navy").save(source)
            spec = render.SceneRenderSpec(
                out_path=root / "clip.mp4", width=64, height=36, fps=12, duration=2,
                source_type="image", source_path=source, video_source="depthflow_3d",
            )
            logs = []
            with patch.object(render, "_run_motion_worker", side_effect=render.RenderError("OpenGL context unavailable")), \
                    patch.object(render, "_render_scene_base", return_value=spec.out_path) as fallback:
                result = render.render_scene(spec, log=logs.append)

        self.assertEqual(result, spec.out_path)
        self.assertIn("built-in local motion renderer", logs[0])
        self.assertEqual(fallback.call_args.args[0].video_source, "original_motion")
        self.assertEqual(render.motion_engine_status()["depthflow_3d"]["state"], "fallback")

    def test_depthflow_worker_caches_result_and_depth_map(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image = root / "source.png"
            Image.new("RGB", (32, 18), "navy").save(image)
            config = {
                "image": str(image), "depth": str(root / "scene_01_depth.png"),
                "output": str(root / "scene_01_3d.mp4"), "preset": "circle", "intensity": "subtle",
                "dof": 0.1, "duration": 1.0, "width": 32, "height": 18, "fps": 8,
            }

            def fake_depth(_image, depth_path, cache_key):
                depth_path.write_bytes(b"cached depth")
                depthflow_worker._write_json(depth_path.with_suffix(".json"), {"cache_key": cache_key})
                return False

            def fake_fallback(_image, output, _config):
                output.write_bytes(b"cached clip")

            with patch.object(depthflow_worker, "_depth_map", side_effect=fake_depth) as depth, \
                    patch.object(depthflow_worker, "_render_depthflow", side_effect=RuntimeError("headless")), \
                    patch.object(depthflow_worker, "_render_ffmpeg_fallback", side_effect=fake_fallback) as fallback:
                first = depthflow_worker.run(config)
                second = depthflow_worker.run(config)

        self.assertEqual(first["engine"], "ffmpeg_parallax_fallback")
        self.assertIn("smooth-eased FFmpeg parallax zoom", first["warning"])
        self.assertTrue(second["cached"])
        self.assertEqual(second["engine"], "ffmpeg_parallax_fallback")
        self.assertIn("DepthFlow unavailable", second["warning"])
        self.assertEqual(depth.call_count, 1)
        self.assertEqual(fallback.call_count, 1)

    def test_cutout_worker_caches_layers_and_rendered_clip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image = root / "source.png"
            Image.new("RGB", (32, 18), "navy").save(image)
            foreground, background, output = root / "foreground.png", root / "background_clean.png", root / "scene.mp4"
            config = {
                "image": str(image), "foreground": str(foreground), "background": str(background),
                "output": str(output), "preset": "dolly_in", "intensity": "medium", "model": "birefnet-general",
                "duration": 1.0, "width": 32, "height": 18, "fps": 8,
            }

            def fake_layers(_image, fg, bg, model):
                fg.write_bytes(b"foreground")
                bg.write_bytes(b"background")
                return model

            def fake_render(_fg, _bg, clip, _config):
                clip.write_bytes(b"clip")

            with patch.object(cutout2d_worker, "build_layers", side_effect=fake_layers) as segment, \
                    patch.object(cutout2d_worker, "_render_layers", side_effect=fake_render) as render_layers:
                first = cutout2d_worker.run(config)
                second = cutout2d_worker.run(config)

        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(segment.call_count, 1)
        self.assertEqual(render_layers.call_count, 1)


if __name__ == "__main__":
    unittest.main()
