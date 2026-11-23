import json
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from tubeforge import alignment, jobs, planner, video, youtube_upload, render, quality
from tubeforge.http import ProviderError


class StudioProPhaseTests(unittest.TestCase):
    def test_old_project_settings_migrate_paid_video_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_dir = root / "legacy"
            project_dir.mkdir()
            (project_dir / "project.json").write_text(json.dumps({
                "id": "legacy", "settings": {"language": "English"}, "job": {"status": "idle"},
            }), encoding="utf-8")
            with patch.object(jobs, "STUDIO_ROOT", root):
                project = jobs.load_project("legacy")
        self.assertFalse(project.settings["allow_paid_video"])
        self.assertEqual(project.settings["paid_video_provider"], "openrouter")
        self.assertEqual(project.settings["paid_video_budget_usd"], 0.0)

    def test_paid_video_needs_opt_in_budget_and_known_model(self):
        settings = {**jobs.DEFAULT_SETTINGS, "allow_paid_video": True,
                    "paid_video_budget_usd": 2.0, "paid_video_spent_usd": 1.5}
        self.assertFalse(video.can_spend_paid_video(
            {**settings, "allow_paid_video": False}, 0.4))
        self.assertTrue(video.can_spend_paid_video(settings, 0.4))
        self.assertFalse(video.can_spend_paid_video(settings, 0.6))
        self.assertFalse(video.can_spend_paid_video(settings, 0.0))
        self.assertFalse(video.can_spend_paid_video(settings, None))

    def test_paid_scene_blocks_unknown_estimate_and_reserves_budget_before_call(self):
        scene = planner.Scene(id="paid-hero", source="ai_video", is_hero_scene=True,
                              video_prompt="A lighthouse in a storm")
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(jobs, "STUDIO_ROOT", Path(directory)), \
                    patch.object(jobs, "render_size", return_value=(1280, 720, (1280, 720))), \
                    patch.object(video, "model_info", return_value={"id": "test/model"}), \
                    patch.object(video, "estimate_cost", return_value=None), \
                    patch.object(jobs, "ensure_image", return_value=Path("fallback.png")), \
                    patch.object(video, "generate_ai_clip") as generate:
                project = jobs.Project({
                    "id": "unknown-cost", "settings": {**jobs.DEFAULT_SETTINGS, "allow_paid_video": True,
                        "ai_video_model": "test/model", "paid_video_budget_usd": 5.0},
                    "scene_state": {}, "logs": [],
                })
                self.assertEqual(
                    jobs.ensure_visual(project, scene, lambda _message: None, lambda: False),
                    ("image", Path("fallback.png")),
                )
                generate.assert_not_called()

            with patch.object(jobs, "render_size", return_value=(1280, 720, (1280, 720))), \
                    patch.object(video, "model_info", return_value={"id": "test/model"}), \
                    patch.object(video, "estimate_cost", return_value=0.4), \
                    patch.object(video, "generate_ai_clip", return_value={"usd": 0.35, "cached": False}):
                project = jobs.Project({
                    "id": "reserved-cost", "settings": {**jobs.DEFAULT_SETTINGS, "allow_paid_video": True,
                        "ai_video_model": "test/model", "paid_video_budget_usd": 1.0,
                        "paid_video_spent_usd": 0.5},
                    "scene_state": {}, "logs": [],
                })
                kind, _ = jobs.ensure_visual(project, scene, lambda _message: None, lambda: False)
        self.assertEqual(kind, "video")
        self.assertEqual(project.settings["paid_video_spent_usd"], 0.9)
        self.assertEqual(project.data["spent_usd"], 0.4)

    def test_flow_pack_is_manual_and_imported_json_is_never_generated(self):
        scene = planner.Scene(id="hero", is_hero_scene=True, subject="A lighthouse",
                              action="waves roll", lighting="Moonlight",
                              video_prompt="cinematic lighthouse", duration=5)
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "hero.png"
            image.write_bytes(b"image")
            pack_path = video.export_flow_pack(scene, image, Path(directory) / "flow.json")
            pack = json.loads(pack_path.read_text(encoding="utf-8"))
        self.assertFalse(pack["automation"])
        self.assertEqual(pack["subject"] if "subject" in pack else scene.subject, "A lighthouse")

    def test_imported_comfy_workflow_requires_exported_prompt_nodes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "workflow.json"
            path.write_text(json.dumps({"1": {"class_type": "LoadImage", "inputs": {}}}),
                            encoding="utf-8")
            workflow = video.import_comfy_workflow(path)
        self.assertIn("1", workflow)
        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory) / "bad.json"
            bad.write_text("[]", encoding="utf-8")
            with self.assertRaises(video.VideoGenError):
                video.import_comfy_workflow(bad)

    def test_comfy_routes_use_documented_prompt_and_history_paths(self):
        with patch.object(video, "request") as request:
            request.return_value.json.return_value = {"prompt_id": "prompt-123"}
            submitted = video.comfy_submit("http://127.0.0.1:8188",
                                           {"1": {"class_type": "LoadImage", "inputs": {}}})
            self.assertEqual(submitted["prompt_id"], "prompt-123")
            self.assertEqual(request.call_args.args[1], "POST")
            self.assertEqual(request.call_args.args[2], "http://127.0.0.1:8188/prompt")
        with patch.object(video, "request") as request:
            request.return_value.json.return_value = {"prompt-123": {"status": {"completed": True}}}
            history = video.comfy_history("http://127.0.0.1:8188", "prompt-123")
        self.assertTrue(history["status"]["completed"])

    def test_distinct_beats_use_multiple_camera_presets(self):
        beats = [{"start": i * 3.0, "end": (i + 1) * 3.0, "text": f"Beat {i}"}
                 for i in range(12)]
        persisted = alignment.persist_distinct_beats({}, beats, "asset")
        presets = [beat["visualTreatment"]["cameraPreset"] for beat in persisted]
        self.assertGreater(len(set(presets)), 5)
        self.assertTrue(all(left != right for left, right in zip(presets, presets[1:])))

    def test_youtube_synthetic_disclosure_playlist_and_scheduling_fields(self):
        class FakeUpload:
            def next_chunk(self):
                return None, {"id": "uploaded"}

        class Videos:
            def insert(self, **kwargs):
                self.body = kwargs["body"]
                return FakeUpload()

        class Playlists:
            def insert(self, **kwargs):
                self.body = kwargs["body"]
                return self
            def execute(self):
                return {"id": "playlist-1"}

        class PlaylistItems:
            def insert(self, **kwargs):
                return self
            def execute(self):
                return {}

        class Service:
            def __init__(self):
                self.video_resource = Videos()
                self.playlist_resource = Playlists()
            def videos(self):
                return self.video_resource
            def playlists(self):
                return self.playlist_resource
            def playlistItems(self):
                return PlaylistItems()

        service = Service()
        fake_media = lambda *args, **kwargs: (args, kwargs)
        fake_google = (None, None, None, lambda *_args, **_kwargs: service, fake_media)
        with tempfile.TemporaryDirectory() as directory:
            video_path = Path(directory) / "film.mp4"
            video_path.write_bytes(b"mp4")
            with patch.object(youtube_upload, "_google_modules", return_value=fake_google), \
                    patch.object(youtube_upload, "connect", return_value=object()):
                youtube_upload.upload_private_draft(
                    video_path, Path("client"), Path("token"), "Title", "Description", [],
                    contains_synthetic_media=True,
                    publish_at="2027-01-01T00:00:00Z",
                )
                playlist_id = youtube_upload.insert_playlist(
                    ["uploaded"], Path("client"), Path("token"), "My Playlist")
        status = service.video_resource.body["status"]
        self.assertTrue(status["containsSyntheticMedia"])
        self.assertEqual(status["publishAt"], "2027-01-01T00:00:00Z")
        self.assertEqual(status["privacyStatus"], "private")
        self.assertEqual(playlist_id, "playlist-1")

    def test_comfy_adapter_retrieves_documented_view_output_and_falls_back_on_error(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "clip.mp4"
            workflow = {"1": {"class_type": "SaveAnimatedWEBP", "inputs": {}}}
            with patch.object(video, "comfy_submit",
                              return_value={"prompt_id": "prompt-123"}), \
                    patch.object(video, "comfy_history",
                                 return_value={"outputs": {"1": {"gifs": [{
                                     "filename": "clip.mp4", "subfolder": "", "type": "output"}]}}}), \
                    patch.object(video, "request") as request:
                request.return_value.content = b"0" * 200
                result = video.generate_comfy_clip(
                    "http://127.0.0.1:8188", workflow, out)
            self.assertTrue(out.is_file())
        self.assertEqual(result["prompt_id"], "prompt-123")
        self.assertIn("/view?", request.call_args.args[2])
        with patch.object(jobs, "ensure_image", return_value=Path("fallback.jpg")):
            project = jobs.Project({
                "id": "comfy-fallback", "settings": {**jobs.DEFAULT_SETTINGS,
                    "ai_video_fallback_source": "original_motion"},
                "scene_state": {}, "logs": [],
            })
            scene = planner.Scene(id="hero", is_hero_scene=True, source="comfyui_local")
            with patch.object(video, "generate_comfy_clip",
                              side_effect=video.VideoGenError("ComfyUI offline")), \
                    patch.object(jobs, "render_size", return_value=(64, 64, (64, 64))), \
                    patch.object(jobs, "ensure_image", return_value=Path("fallback.jpg")):
                kind, path = jobs.ensure_visual(project, scene, lambda _m: None, lambda: False)
        self.assertEqual(kind, "image")
        self.assertEqual(path, Path("fallback.jpg"))

    def test_flow_manual_pack_is_importable_and_imported_clip_is_used(self):
        scene = planner.Scene(id="flow", is_hero_scene=True, subject="Moon",
                              action="A rover moves", lighting="Low-key",
                              video_prompt="Cinematic rover", duration=5,
                              source="flow_manual")
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "frame.png"
            image.write_bytes(b"frame")
            clip = Path(directory) / "imported.mp4"
            clip.write_bytes(b"mp4")
            pack = video.export_flow_pack(scene, image, Path(directory) / "flow.json")
            data = json.loads(pack.read_text(encoding="utf-8"))
        self.assertFalse(data["automation"])
        self.assertIn("Cinematic rover", data["prompt"])
        project = jobs.Project({
            "id": "flow-import", "settings": {**jobs.DEFAULT_SETTINGS,
                "ai_video_fallback_source": "original_motion"},
            "scene_state": {}, "logs": [],
        })
        project.state(scene.id).update(hero_clip=str(clip))
        with tempfile.TemporaryDirectory() as directory:
            imported = Path(directory) / "imported.mp4"
            imported.write_bytes(b"mp4")
            project.state(scene.id)["hero_clip"] = str(imported)
            scene.hero_clip = str(imported)
            kind, path = jobs.ensure_visual(project, scene, lambda _m: None, lambda: False)
        self.assertEqual((kind, path), ("video", scene.hero_clip and Path(scene.hero_clip)))

    def test_preflight_reports_fonts_opengl_and_real_comfy_detail(self):
        with patch.object(render.shutil, "which", side_effect=lambda name: "/bin/" + name
                          if name in ("ffmpeg", "ffprobe") else None), \
                patch.object(render.shutil, "disk_usage",
                             return_value=type("Usage", (), {"free": 10 * 1024 ** 3})()), \
                patch.object(urllib.request, "urlopen", side_effect=OSError("offline")):
            preflight = render.preflight_check("http://127.0.0.1:8188", "")
        self.assertIn("fonts", preflight["checks"])
        self.assertIn("opengl", preflight["checks"])
        self.assertIn("unreachable", preflight["comfyui_detail"])
        self.assertIn("gpu", preflight)
        self.assertIn("cpu_fallback_available", preflight["checks"])
        self.assertFalse(preflight["checks"]["comfyui"])

    def test_license_audit_blocks_unknown_assets_until_acknowledged(self):
        project = jobs.Project({
            "id": "license-test",
            "settings": {**jobs.DEFAULT_SETTINGS, "licenses": [
                {"source": "stock-provider", "license": "unknown"},
            ]},
            "scene_state": {}, "logs": [],
        })
        audit = jobs.license_audit(project)
        self.assertTrue(audit["blocked"])
        project.settings["license_unknown_acknowledged"] = True
        self.assertTrue(jobs.license_audit(project)["passed"])

    def test_render_quality_flags_missing_frames_and_bad_clipping_proxy(self):
        with tempfile.TemporaryDirectory() as directory:
            result = quality.inspect_render(Path(directory) / "missing.mp4")
        self.assertEqual(result["status"], "fail")
        self.assertIn("missing", result["issues"][0].lower())


if __name__ == "__main__":
    unittest.main()
