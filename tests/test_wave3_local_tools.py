import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tubeforge import batch_queue, broll_vault, jobs, local_ai, mla_dubbing, planner, thumbnail_lab
from workers import blender_worker, manim_worker


class Wave3LocalToolsTests(unittest.TestCase):
    def test_vault_indexes_by_content_and_copies_selected_asset(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "city_skyline.mp4"
            source.write_bytes(b"local media")
            with patch.object(broll_vault, "VAULT_ROOT", root / "vault"), \
                    patch.object(broll_vault, "INDEX_PATH", root / "vault" / "index.json"):
                indexed = broll_vault.index_files([source], ["night footage"])
                results = broll_vault.search("night footage", kind="video")
                copied = broll_vault.copy_to_project(source, root / "project")
            self.assertEqual(len(indexed), 1)
            self.assertEqual(results[0]["sha256"], indexed[0]["sha256"])
            self.assertEqual(copied.read_bytes(), source.read_bytes())

    def test_vault_semantic_candidates_can_match_without_tag_overlap(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "forest.png"
            source.write_bytes(b"image data")
            with patch.object(broll_vault, "VAULT_ROOT", root / "vault"), \
                    patch.object(broll_vault, "INDEX_PATH", root / "vault" / "index.json"):
                row = broll_vault.index_files([source], ["trees"])[0]
                with patch.object(broll_vault, "_semantic_scores", return_value={row["sha256"]: 0.9}):
                    results = broll_vault.search("astronaut in space")
            self.assertEqual(results[0]["sha256"], row["sha256"])

    def test_batch_queue_recovers_interrupted_items_as_pending(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "queue.json"
            path.write_text(json.dumps({
                "version": 1, "items": [{"project_id": "p1", "status": "running"}],
            }), encoding="utf-8")
            with patch.object(batch_queue, "QUEUE_PATH", path), patch.object(batch_queue, "_RUNNER", None):
                snapshot = batch_queue.snapshot()
            self.assertEqual(snapshot[0]["status"], "pending")
            self.assertIn("Recovered", snapshot[0]["message"])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["items"][0]["status"], "pending")

    def test_thumbnail_lab_caches_processed_image_and_feed_preview(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.png"
            Image.new("RGB", (320, 180), (20, 90, 150)).save(source)
            first, warning = thumbnail_lab.process_thumbnail(source, root / "cache")
            second, _ = thumbnail_lab.process_thumbnail(source, root / "cache")
            preview = thumbnail_lab.mobile_preview(first, "dark")
            self.assertIsNone(warning)
            self.assertEqual(first, second)
            self.assertEqual(preview.size, (184, 142))

    def test_local_ai_adapters_reject_non_loopback_services(self):
        with patch.object(local_ai.config, "secret", return_value="http://127.0.0.1:8000"):
            self.assertEqual(local_ai.service_url("qwen3_tts"), "http://127.0.0.1:8000")
        with patch.object(local_ai.config, "secret", return_value="https://example.com"):
            with self.assertRaisesRegex(ValueError, "loopback"):
                local_ai.service_url("qwen3_tts")

    def test_chatterbox_dubbing_forwards_project_consent_and_reference(self):
        project = SimpleNamespace(settings={
            "local_voice_reference_path": "project/audio/reference.wav",
            "local_voice_clone_consent": True,
        })
        self.assertEqual(mla_dubbing._voice_synthesis_options(project, "chatterbox"), {
            "voice_reference_path": Path("project/audio/reference.wav"),
            "voice_clone_consent": True,
        })
        self.assertEqual(mla_dubbing._voice_synthesis_options(project, "qwen3_tts"), {})

    def test_optional_worker_configs_are_bounded_and_template_only(self):
        config = manim_worker.validate_config({
            "template": "compounding_curve", "duration": 900, "width": 9000, "annual_rate": 4,
        })
        self.assertEqual(config["duration"], 30.0)
        self.assertEqual(config["width"], 3840)
        self.assertEqual(config["annual_rate"], 1.0)
        with self.assertRaisesRegex(ValueError, "template"):
            manim_worker.validate_config({"template": "run_arbitrary_code"})

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root / "outside.gltf"
            outside.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "inside the current project"):
                blender_worker.validate_config({
                    "project_root": str(root / "project"), "model_path": str(outside),
                    "output": str(root / "out.mp4"),
                })

    def test_scene_workers_enter_normal_video_postprocessing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for engine in ("manim_math", "blender_3d"):
                scene = planner.Scene(
                    id=f"scene-{engine}", title="Local scene", duration=3,
                    videoSource=engine, proceduralProps={"worker_template": "equation_transform"},
                )
                plan = planner.CinematicPlan(scenes=[scene])
                with patch.object(jobs, "STUDIO_ROOT", root):
                    project = jobs.Project({
                        "id": f"project-{engine}", "settings": dict(jobs.DEFAULT_SETTINGS),
                        "plan": plan.model_dump(by_alias=True),
                        "scene_state": {scene.id: {"seed": 1}},
                    })
                    source = root / "still.png"
                    source.write_bytes(b"still")
                    spec = SimpleNamespace(
                        out_path=root / f"{engine}.mp4", width=320, height=180, fps=24,
                        source_type="image", source_path=source, video_source=engine, camera="Dolly",
                    )

                    def run_worker(config):
                        output = Path(config["output"])
                        output.parent.mkdir(parents=True, exist_ok=True)
                        output.write_bytes(b"worker video")
                        return {"output": str(output)}

                    worker = jobs.manim_worker if engine == "manim_math" else jobs.blender_worker
                    with patch.object(jobs, "scene_transition", return_value=("", 0.0)), \
                            patch.object(jobs, "ensure_visual", return_value=("image", source)), \
                            patch.object(jobs, "ensure_voice", return_value=[]), \
                            patch.object(jobs, "scene_length", return_value=3.0), \
                            patch.object(jobs, "caption_chunks", return_value=[]), \
                            patch.object(jobs, "render_spec", return_value=spec), \
                            patch.object(jobs, "spec_fingerprint", return_value="fingerprint"), \
                            patch.object(worker, "run", side_effect=run_worker), \
                            patch.object(jobs.render, "render_scene") as render_scene, \
                            patch.object(jobs.render, "video_is_decodable", return_value=True):
                        render_scene.side_effect = lambda received, **_kwargs: received.out_path.write_bytes(b"final")
                        outcome = jobs.produce_scene(project, scene, 0, 1, jobs.threading.Event())
                    self.assertEqual(outcome, {"len": 3.0})
                    received = render_scene.call_args.args[0]
                    self.assertEqual(received.source_type, "video")
                    self.assertEqual(received.source_path.name, f"{engine.replace('_math', '').replace('_3d', '')}_worker.mp4")
                    self.assertEqual(received.video_source, "original_motion")
                    self.assertEqual(received.camera, "Static (locked off)")


if __name__ == "__main__":
    unittest.main()
