import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from tubeforge import cache, images, jobs, llm, planner, video
from tubeforge.http import ProviderError


def _image_bytes():
    import io

    buffer = io.BytesIO()
    Image.effect_noise((256, 256), 100).convert("RGB").save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


class PhaseThreeTests(unittest.TestCase):
    def test_gemini_routing_uses_high_reasoning_only_for_creative_tasks(self):
        with patch.object(llm.config, "gemini_key", return_value="configured"):
            self.assertEqual(llm._backend_models("gemini", task="fast"),
                             ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"])
            self.assertEqual(
                llm._backend_models("gemini", task="creative"),
                ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite",
                 "gemini-3.1-flash-lite"],
            )
        self.assertNotIn("gemini-2.5-flash", llm.GEMINI_MODELS)

    def test_gemini_local_daily_cap_is_reserved_before_http_request(self):
        with patch.object(cache, "reserve_daily_calls", return_value=False) as reserve, \
                patch.object(llm, "request") as http_request:
            with self.assertRaises(ProviderError) as raised:
                llm._call_gemini("gemini-3.5-flash-lite", "sys", "user", 100, 0.1, True, 15)
        self.assertEqual(raised.exception.status, 429)
        reserve.assert_called_once_with("gemini:gemini-3.5-flash-lite", 500)
        http_request.assert_not_called()

    def test_groq_429_rotates_to_the_next_free_model(self):
        calls = []

        def groq(model, *_args):
            calls.append(model)
            if model == "qwen/qwen3.8-27b":
                raise ProviderError("groq", "HTTP 429: model rate limit", status=429, fatal=True)
            return '{"ok": true}'

        callers = {"groq": groq, "openrouter": lambda *_args: '{"ok": true}',
                   "gemini": lambda *_args: '{"ok": true}'}
        with patch.dict(llm.TASK_ORDER, {"fast": ["groq"]}), \
                patch.object(llm, "_backend_models", return_value=["qwen/qwen3.8-27b", "openai/gpt-oss-120b"]), \
                patch.object(llm, "_CALLERS", callers), \
                patch.object(llm, "trip_breaker") as breaker:
            data, model = llm.complete_json("sys", "user", use_cache=False)
        self.assertEqual(data, {"ok": True})
        self.assertEqual(calls, ["qwen/qwen3.8-27b", "openai/gpt-oss-120b"])
        self.assertEqual(model, "groq/openai/gpt-oss-120b")
        breaker.assert_called_once()

    def test_flux_schnell_estimate_uses_published_tile_and_step_rates(self):
        self.assertEqual(images.AUTO_ORDER[0], "cf_flux1_schnell")
        expected = 4.8 * 6 + 9.6 * 4
        self.assertAlmostEqual(images.estimate_neurons("cf_flux1_schnell", 1536, 864, steps=4), expected)
        self.assertAlmostEqual(images.estimate_neurons("cf_klein4b", 1_000, 1_000), 1364)

    def test_flux_schnell_generation_reserves_estimate_and_releases_on_failure(self):
        image_data = _image_bytes()
        with tempfile.TemporaryDirectory() as temp_dir:
            out = Path(temp_dir) / "scene.jpg"
            with patch.object(images, "engine_available", side_effect=lambda key: (key == "cf_flux1_schnell", "")), \
                    patch.object(images, "_cf_flux1_schnell", return_value=image_data) as generate, \
                    patch.object(cache, "reserve_cloudflare_neurons", return_value=True) as reserve, \
                    patch.object(cache, "get_bytes", return_value=None), \
                    patch.object(cache, "put_bytes"), \
                    patch.object(cache, "record_usage"):
                result = images.generate_image("cinematic frame", out, 1536, 864, seed=7,
                                               engine="cf_flux1_schnell", allow_stock=False)
            self.assertEqual(result.engine, "cf_flux1_schnell")
            self.assertAlmostEqual(result.neurons, 67.2)
            reserve.assert_called_once()
            self.assertAlmostEqual(reserve.call_args.args[0], 67.2)
            generate.assert_called_once()

            with patch.object(images, "engine_available",
                              side_effect=lambda key: (key == "cf_flux1_schnell", "")), \
                    patch.object(images, "_cf_flux1_schnell",
                                 side_effect=ProviderError("cloudflare", "offline")), \
                    patch.object(cache, "reserve_cloudflare_neurons", return_value=True), \
                    patch.object(cache, "get_bytes", return_value=None), \
                    patch.object(cache, "release_cloudflare_neurons") as release:
                with self.assertRaises(images.ImageGenError):
                    images.generate_image("failed frame", out, 1536, 864, seed=8,
                                          engine="cf_flux1_schnell", allow_stock=False)
            release.assert_called_once()
            self.assertAlmostEqual(release.call_args.args[0], 67.2)

    def test_pexels_video_search_requests_orientation_and_maps_credits(self):
        payload = {"videos": [
            {"id": 42, "width": 1080, "height": 1920, "url": "https://pexels.com/video/42",
             "image": "https://pexels.test/thumb.jpg", "duration": 12,
             "user": {"name": "Artist"}, "video_files": [
                 {"file_type": "video/mp4", "width": 1080, "height": 1920,
                  "link": "https://cdn.pexels.test/42.mp4"}]},
            {"id": 43, "width": 1920, "height": 1080, "url": "https://pexels.com/video/43",
             "image": "https://pexels.test/thumb2.jpg", "duration": 8,
             "user": {"name": "Other"}, "video_files": [
                 {"file_type": "video/mp4", "width": 1920, "height": 1080,
                  "link": "https://cdn.pexels.test/43.mp4"}]},
        ]}
        response = SimpleNamespace(
            headers={"X-Ratelimit-Limit": "20000", "X-Ratelimit-Remaining": "19999",
                     "X-Ratelimit-Reset": "1800000000"},
            json=lambda: payload,
        )
        with patch.object(video.config, "pexels_key", return_value="key"), \
                patch.object(video.config, "pixabay_key", return_value=""), \
        patch.object(video.cache, "get_json", return_value=None), \
        patch.object(video.cache, "put_json"), \
                patch.object(cache, "reserve_provider_request", return_value=True) as reserve, \
                patch.object(video, "request", return_value=response) as http_request, \
                patch.object(cache, "record_provider_quota") as record_quota, \
                patch.object(cache, "record_usage"):
            clips = video.search_stock_videos("forest", portrait=True, min_width=720)
        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0].provider, "Pexels")
        self.assertEqual(clips[0].id, "pexels-42")
        self.assertIn("portrait", http_request.call_args.kwargs["params"]["orientation"])
        reserve.assert_called_once_with("pexels", 200, 20000)
        record_quota.assert_called_once_with("pexels", 20000, 19999, 1800000000)

    def test_pexels_photo_search_selects_orientation_and_includes_attribution(self):
        response = SimpleNamespace(json=lambda: {"photos": [
            {"id": 3, "photographer": "Artist", "url": "https://pexels.com/photo/3",
             "src": {"landscape": "https://cdn.pexels.test/landscape.jpg"}},
        ]})
        with patch.object(images.cache, "get_json", return_value=None), \
                patch.object(images, "_pexels_request", return_value=response) as search, \
                patch.object(images, "request", return_value=SimpleNamespace(content=_image_bytes())) as download:
            data, credit = images._pexels_photo("mountain", 1920, 1080, 0)
        self.assertTrue(data)
        self.assertIn("Artist", credit)
        self.assertIn("Pexels", credit)
        self.assertEqual(search.call_args.args[1]["orientation"], "landscape")
        self.assertEqual(download.call_args.args[2], "https://cdn.pexels.test/landscape.jpg")

    def test_visual_bible_generates_references_and_resumes_completed_work(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(jobs, "STUDIO_ROOT", root):
                settings = {**jobs.DEFAULT_SETTINGS, "image_engine": "auto", "channel_name": "Test Channel"}
                project_data = {
                    "id": "visual-bible-test", "idea": "A story", "settings": settings,
                    "visualBible": jobs._default_visual_bible("visual-bible-test"),
                    "plan": None, "scene_state": {}, "logs": [], "job": {"status": "idle"},
                }
                project = jobs.Project(project_data)
                project.set_plan(planner.CinematicPlan(
                    visual_identity="Warm amber, teal shadows",
                    characters=[planner.Character(name="Mira", look="Short dark hair, red coat")],
                    scenes=[planner.Scene(id="scene-1", characters=["Mira"], image_prompt="A city at night")],
                ))
                calls = []

                def fake_generate(prompt, out_path, width, height, **kwargs):
                    calls.append((prompt, Path(out_path), kwargs))
                    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
                    Path(out_path).write_bytes(_image_bytes())
                    return images.ImageResult(Path(out_path), "cf_klein4b", "flux-2-klein-4b",
                                              width, height, neurons=40)

                with patch.object(images, "engine_available", return_value=(False, "missing key")), \
                        patch.object(images, "generate_image", side_effect=fake_generate):
                    bible = jobs.generate_visual_bible(project)
                    self.assertFalse(bible["approved"])
                    self.assertEqual(len(calls), 2)
                    self.assertIn("Warm amber", calls[0][0])
                    self.assertEqual(len(calls[1][2]["refs"]), 1)
                    self.assertEqual(calls[1][2]["refs"][0], Path(bible["masterReferencePath"]))
                    jobs.generate_visual_bible(project)
                self.assertEqual(len(calls), 2)
                self.assertTrue(Path(bible["characters"][0]["referenceImagePath"]).is_file())
                self.assertEqual(project.settings["char_refs"]["Mira"],
                                 bible["characters"][0]["referenceImagePath"])

    def test_visual_bible_sync_uses_newer_uploaded_legacy_character_reference(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old_ref = root / "old.jpg"
            new_ref = root / "uploaded.jpg"
            old_ref.write_bytes(_image_bytes())
            new_ref.write_bytes(_image_bytes())
            bible = jobs._default_visual_bible("legacy-ref-test")
            bible["characters"] = [{
                "id": "mira", "name": "Mira", "description": "old look",
                "referenceImagePath": str(old_ref),
            }]
            project = jobs.Project({
                "id": "legacy-ref-test", "settings": {**jobs.DEFAULT_SETTINGS,
                    "char_refs": {"Mira": str(new_ref)}},
                "visualBible": bible,
                "plan": planner.CinematicPlan(characters=[
                    planner.Character(name="Mira", look="updated look")]).model_dump(),
            })
            updated = jobs.ensure_visual_bible(project)
        self.assertEqual(updated["characters"][0]["referenceImagePath"], str(new_ref))
        self.assertEqual(updated["characters"][0]["description"], "updated look")

    def test_approved_visual_bible_is_used_for_four_scenes_and_cached(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(jobs, "STUDIO_ROOT", root):
                settings = {**jobs.DEFAULT_SETTINGS, "image_engine": "cf_klein4b"}
                project = jobs.Project({
                    "id": "locked-visual-test", "idea": "A story", "settings": settings,
                    "visualBible": jobs._default_visual_bible("locked-visual-test"),
                    "plan": None, "scene_state": {}, "logs": [],
                })
                master = root / "master.jpg"
                character_ref = root / "character.jpg"
                master.write_bytes(_image_bytes())
                character_ref.write_bytes(_image_bytes())
                project.data["visualBible"].update(
                    approved=True, stylePrefix="locked teal and amber palette",
                    masterReferencePath=str(master), globalNegativePrompt="no text, no watermark",
                    seedBase=12345,
                    characters=[{"id": "mira", "name": "Mira", "description": "red coat",
                                 "referenceImagePath": str(character_ref)}],
                )
                project.settings["char_refs"] = {"Mira": str(character_ref)}
                scenes = [planner.Scene(id=f"scene-{index}", characters=["Mira"], image_prompt=f"Frame {index}")
                          for index in range(4)]
                project.set_plan(planner.CinematicPlan(characters=[
                    planner.Character(name="Mira", look="red coat")], scenes=scenes))
                calls = []

                def fake_generate(prompt, out_path, width, height, **kwargs):
                    calls.append((prompt, kwargs))
                    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
                    Path(out_path).write_bytes(_image_bytes())
                    return images.ImageResult(Path(out_path), "cf_klein4b", "flux-2-klein-4b", width, height)

                with patch.object(jobs, "render_size", return_value=(64, 64, (64, 64))), \
                        patch.object(images, "generate_image", side_effect=fake_generate):
                    for scene in scenes:
                        jobs.ensure_image(project, scene, lambda _message: None)
                    for scene in scenes:
                        jobs.ensure_image(project, scene, lambda _message: None)
                self.assertEqual(len(calls), 4)
                self.assertTrue(all("locked teal and amber palette" in prompt for prompt, _ in calls))
                self.assertEqual([kwargs["seed"] for _, kwargs in calls],
                                 [12345 + index * 104729 for index in range(4)])
                self.assertTrue(all(kwargs["refs"][0] == master and kwargs["refs"][1] == character_ref
                                    for _, kwargs in calls))

    def test_four_cloudflare_scene_images_update_neurons_and_resume_from_cache(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project_root = root / "projects"
            usage_root = root / "usage"
            cache_root = root / "cache"
            with patch.object(jobs, "STUDIO_ROOT", project_root), \
                    patch.object(cache, "OUTPUT_ROOT", usage_root), \
                    patch.object(cache, "USAGE_FILE", usage_root / "_usage.json"), \
                    patch.object(cache, "CACHE_DIR", cache_root):
                settings = {**jobs.DEFAULT_SETTINGS, "image_engine": "cf_klein4b"}
                project = jobs.Project({
                    "id": "cloudflare-visual-test", "idea": "A story", "settings": settings,
                    "visualBible": jobs._default_visual_bible("cloudflare-visual-test"),
                    "plan": None, "scene_state": {}, "logs": [],
                })
                master = root / "master.jpg"
                character_ref = root / "character.jpg"
                master.write_bytes(_image_bytes())
                character_ref.write_bytes(_image_bytes())
                project.data["visualBible"].update(
                    approved=True, stylePrefix="consistent copper and blue lighting",
                    masterReferencePath=str(master), seedBase=31415,
                    characters=[{"id": "mira", "name": "Mira", "description": "red coat",
                                 "referenceImagePath": str(character_ref)}],
                )
                project.settings["char_refs"] = {"Mira": str(character_ref)}
                scenes = [planner.Scene(id=f"cloud-scene-{index}", characters=["Mira"],
                                        image_prompt=f"Cloud scene {index}", duration=6)
                          for index in range(4)]
                project.set_plan(planner.CinematicPlan(
                    characters=[planner.Character(name="Mira", look="red coat")], scenes=scenes))
                with patch.object(jobs, "render_size", return_value=(64, 64, (64, 64))), \
                        patch.object(images, "engine_available", return_value=(True, "")), \
                        patch.object(images, "_cf_flux2", return_value=_image_bytes()) as generate:
                    for scene in scenes:
                        jobs.ensure_image(project, scene, lambda _message: None)
                    used_after_first_pass = cache.usage_today()["cloudflare_neurons"]
                    for scene in scenes:
                        jobs.ensure_image(project, scene, lambda _message: None)
                    self.assertEqual(generate.call_count, 4)
                    self.assertEqual(cache.usage_today()["cloudflare_neurons"], used_after_first_pass)
                width, height = images.generation_size(64, 64, "cf_klein4b")
                per_frame = images.estimate_neurons("cf_klein4b", width, height, ref_tiles=[1, 1])
                self.assertAlmostEqual(used_after_first_pass, per_frame * 4)
                self.assertEqual(cache.usage_today()["calls"]["cloudflare"], 4)

    def test_legacy_project_receives_nonblocking_visual_bible_schema(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project_dir = root / "legacy-phase3"
            project_dir.mkdir()
            (project_dir / "project.json").write_text(json.dumps({
                "id": "legacy-phase3", "idea": "Old project", "settings": {"style": "Classic"},
                "plan": None, "scene_state": {}, "logs": [], "job": {"status": "idle"},
            }), encoding="utf-8")
            with patch.object(jobs, "STUDIO_ROOT", root):
                jobs._OPEN.pop("legacy-phase3", None)
                project = jobs.load_project("legacy-phase3")
            self.assertEqual(project.data["visualBible"]["visualMode"], "stylized_3d")
            self.assertFalse(project.data["visualBible"]["required"])
            self.assertEqual(project.settings["style"], "Classic")
            self.assertIn("visualBible", json.loads((project_dir / "project.json").read_text(encoding="utf-8")))
            jobs._OPEN.pop("legacy-phase3", None)

    def test_legacy_scene_image_cache_fingerprint_remains_compatible(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = dict(jobs.DEFAULT_SETTINGS)
            scene = planner.Scene(id="legacy-scene", title="Legacy", image_prompt="A mountain")
            plan = planner.CinematicPlan(scenes=[scene])
            project = jobs.Project({
                "id": "legacy-image-cache", "idea": "Old project", "settings": settings,
                "plan": plan.model_dump(), "scene_state": {}, "logs": [],
            })
            state = project.state(scene.id)
            state["seed"] = 123
            width, height, _ = jobs.render_size(settings)
            old_fingerprint = jobs._fp(
                "img", jobs.build_image_prompt(project, scene), jobs._negative(project, scene),
                settings["image_engine"], state["seed"], width, height, [],
                settings.get("openrouter_image_model"), settings["allow_paid"], settings["allow_stock"],
            )
            image_path = root / project.id / "scenes" / scene.id / "image.jpg"
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(b"cached legacy image")
            state.update(image=str(image_path), image_fp=old_fingerprint)

            with patch.object(jobs, "STUDIO_ROOT", root), \
                    patch.object(jobs, "render_size", return_value=(width, height, (width, height))), \
                    patch.object(images, "generate_image") as generate:
                self.assertEqual(jobs.ensure_image(project, scene, lambda _message: None), image_path)
            generate.assert_not_called()

    def test_provider_request_tracker_enforces_pexels_hour_and_month(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(cache, "PROVIDER_QUOTA_FILE", root / "quotas.json"), \
                    patch.object(cache, "OUTPUT_ROOT", root):
                self.assertTrue(cache.reserve_provider_request("pexels", 1, 20000))
                self.assertFalse(cache.reserve_provider_request("pexels", 1, 20000))
                cache.record_provider_quota("pexels", 20000, 123, 1800000000)
                status = cache.provider_quota("pexels")
        self.assertEqual(status["hourly_used"], 1)
        self.assertEqual(status["remote_month_remaining"], 123)


if __name__ == "__main__":
    unittest.main()
