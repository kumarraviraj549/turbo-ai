import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw

from tubeforge import cache, images, jobs, llm, planner, quality, render


class FreeModeTests(unittest.TestCase):
    def test_paid_image_engine_is_rejected_before_provider_access(self):
        with patch.object(images, "_openrouter_image") as paid_call:
            with self.assertRaises(images.ImageGenError):
                images.generate_image("test", Path("unused.jpg"), 64, 64, engine="openrouter")
        paid_call.assert_not_called()

    def test_openrouter_preference_cannot_select_paid_model(self):
        with patch.object(llm.config, "openrouter_key", return_value="key"), \
                patch.object(llm, "openrouter_free_chain", return_value=["vendor/model:free"]):
            self.assertEqual(llm._backend_models("openrouter", "vendor/paid-model"),
                             ["vendor/model:free"])
            self.assertEqual(llm._backend_models("openrouter", "vendor/other:free"),
                             ["vendor/other:free", "vendor/model:free"])

    def test_plan_receives_user_research_notes_before_generation(self):
        class FakeProject:
            data = {"idea": "A test story", "job": {}}
            settings = {
                **jobs.DEFAULT_SETTINGS,
                "research_notes": "Fact: the event happened in 2024. Source: https://example.test",
            }
            lock = threading.RLock()

            def save(self, **_kwargs):
                pass

            def set_plan(self, value):
                self.plan = value

            def log(self, _message):
                pass

        plan = planner.CinematicPlan(scenes=[planner.Scene(id="scene", title="A scene")])
        with patch.object(planner, "generate_plan", return_value=plan) as generate:
            jobs.run_plan(FakeProject(), threading.Event())
        self.assertIn("https://example.test", generate.call_args.args[8])
        self.assertEqual(plan.research_sources, ["Fact: the event happened in 2024. Source: https://example.test"])

    def test_cloudflare_reservations_enforce_daily_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(cache, "USAGE_FILE", Path(tmp) / "usage.json"), \
                    patch.object(cache, "OUTPUT_ROOT", Path(tmp)):
                self.assertTrue(cache.reserve_cloudflare_neurons(7000))
                self.assertFalse(cache.reserve_cloudflare_neurons(3001))
                self.assertEqual(cache.usage_today()["cloudflare_neurons"], 7000)

    def test_visual_qa_flags_duplicate_stills_and_missing_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "scene.jpg"
            image = Image.new("RGB", (128, 128), "black")
            draw = ImageDraw.Draw(image)
            draw.rectangle((20, 20, 100, 100), fill="white")
            image.save(image_path)
            scenes = [
                SimpleNamespace(id="one", title="One", characters=[],
                                source="original_motion"),
                SimpleNamespace(id="two", title="Two", characters=[],
                                source="original_motion"),
                SimpleNamespace(id="three", title="Three", characters=[],
                                source="original_motion"),
            ]
            result = quality.inspect_project(
                SimpleNamespace(scenes=scenes),
                {"one": {"image": str(image_path)}, "two": {"image": str(image_path)}},
                {},
                (64, 64),
            )
        self.assertEqual(result["status"], "review")
        self.assertTrue(any("nearly identical" in issue for issue in result["issues"]))
        self.assertTrue(any("Only 2 of 3" in issue for issue in result["issues"]))

    def test_legacy_ai_video_scene_uses_local_image_motion(self):
        class FakeProject:
            settings = {}

            def state(self, _scene_id):
                return {"seed": 1}

            def scene_dir(self, _scene_id):
                return Path(".")

        scene = SimpleNamespace(id="legacy", title="Legacy", source="ai_video")
        image_path = Path("local-image.jpg")
        with patch.object(jobs, "render_size", return_value=(64, 64, (64, 64))), \
                patch.object(jobs, "ensure_image", return_value=image_path) as make_image:
            kind, path = jobs.ensure_visual(FakeProject(), scene, lambda _msg: None, lambda: False)
        self.assertEqual((kind, path), ("image", image_path))
        make_image.assert_called_once()

    def test_pipeline_version_invalidates_cached_scene_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "scene.jpg"
            source.write_bytes(b"source")
            spec = render.SceneRenderSpec(
                out_path=Path(tmp) / "clip.mp4",
                width=64,
                height=64,
                fps=24,
                duration=1,
                source_type="image",
                source_path=source,
            )
            previous = jobs.spec_fingerprint(spec)
            with patch.object(render, "PIPELINE_VERSION", "test-next-version"):
                updated = jobs.spec_fingerprint(spec)
        self.assertNotEqual(previous, updated)


if __name__ == "__main__":
    unittest.main()
