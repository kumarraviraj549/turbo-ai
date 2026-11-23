import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from tubeforge import alignment, cache, jobs, llm, planner, tts, youtube_upload


def _record_usage_worker(output_root, usage_path, count):
    cache.OUTPUT_ROOT = Path(output_root)
    cache.USAGE_FILE = Path(usage_path)
    cache.USAGE_BACKUP_FILE = Path(str(usage_path) + ".bak")
    for _ in range(count):
        cache.record_usage("worker")


class PhaseZeroFixTests(unittest.TestCase):
    def test_groq_alignment_request_uses_replayable_file_bytes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "voice.wav"
            path.write_bytes(b"audio payload")
            response = SimpleNamespace(json=lambda: {"words": [{"word": "hi", "start": 0.1, "end": 0.3}]})
            with patch.object(alignment.config, "groq_key", return_value="key"), \
                    patch.object(alignment, "request", return_value=response) as request:
                self.assertEqual(alignment._groq_one(path, None)[0]["word"], "hi")
            self.assertEqual(request.call_args.kwargs["files"]["file"], (path.name, b"audio payload"))

    def test_usage_writes_a_recoverable_backup_on_corruption(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            usage_path = root / "_usage.json"
            with patch.object(cache, "OUTPUT_ROOT", root), \
                    patch.object(cache, "USAGE_FILE", usage_path), \
                    patch.object(cache, "USAGE_BACKUP_FILE", root / "_usage.json.bak"):
                cache.record_usage("provider")
                cache.record_usage("provider")
                expected = cache.usage_today()
                usage_path.write_text("{", encoding="utf-8")
                self.assertEqual(cache.usage_today(), expected)
                self.assertTrue(list(root.glob("_usage.json.corrupt-*")))
                self.assertFalse(list(root.glob("*.tmp")))

    def test_usage_file_lock_preserves_updates_across_processes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            usage_path = root / "_usage.json"
            context = multiprocessing.get_context("spawn")
            workers = [
                context.Process(target=_record_usage_worker, args=(str(root), str(usage_path), 8))
                for _ in range(3)
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(30)
                self.assertEqual(worker.exitcode, 0)
            with patch.object(cache, "OUTPUT_ROOT", root), \
                    patch.object(cache, "USAGE_FILE", usage_path), \
                    patch.object(cache, "USAGE_BACKUP_FILE", root / "_usage.json.bak"):
                self.assertEqual(cache.usage_today()["calls"]["worker"], 24)

    def test_corrupt_usage_without_a_backup_fails_explicitly(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            usage_path = root / "_usage.json"
            usage_path.write_text("{", encoding="utf-8")
            with patch.object(cache, "OUTPUT_ROOT", root), \
                    patch.object(cache, "USAGE_FILE", usage_path), \
                    patch.object(cache, "USAGE_BACKUP_FILE", root / "_usage.json.bak"):
                with self.assertRaisesRegex(RuntimeError, "no backup exists"):
                    cache.usage_today()

    def test_cloudflare_is_not_an_llm_backend(self):
        self.assertNotIn("cloudflare", llm._CALLERS)
        self.assertTrue(all("cloudflare" not in providers for providers in llm.TASK_ORDER.values()))

    def test_edge_tts_is_rejected_for_production_synthesis(self):
        with self.assertRaisesRegex(tts.TTSError, "draft-preview only"):
            tts.synthesize("Final narration", Path("unused.mp3"), engine="edge")

    def test_kokoro_model_download_checks_the_pinned_digest(self):
        payload = b"x" * 2048
        expected = tts.hashlib.sha256(payload).hexdigest()

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, size=-1):
                nonlocal payload
                result, payload = payload[:size], payload[size:]
                return result

        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "model.bin"
            with patch("urllib.request.urlopen", return_value=FakeResponse()):
                self.assertEqual(tts._download_model_file("https://example.invalid/model", destination, expected),
                                 destination)
            self.assertEqual(tts._file_sha256(destination), expected)

    def test_kokoro_model_download_rejects_bad_digest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "model.bin"
            with patch("urllib.request.urlopen") as urlopen:
                urlopen.return_value.__enter__.return_value.read.side_effect = [b"x" * 2048, b""]
                with self.assertRaisesRegex(tts.TTSError, "SHA-256"):
                    tts._download_model_file("https://example.invalid/model", destination, "0" * 64)
            self.assertFalse(destination.exists())

    def test_thumbnail_mime_type_uses_image_content(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "thumbnail.bin"
            Image.new("RGB", (8, 8), "blue").save(path, format="PNG")
            self.assertEqual(youtube_upload._thumbnail_mime_type(path), "image/png")

    def test_resumable_upload_retries_transient_http_error(self):
        from googleapiclient.errors import HttpError

        class Upload:
            def __init__(self):
                self.calls = 0

            def next_chunk(self):
                self.calls += 1
                if self.calls == 1:
                    raise HttpError(SimpleNamespace(status=503, reason="unavailable"), b"temporary failure")
                return "status", {"id": "video"}

        upload = Upload()
        with patch.object(youtube_upload.time, "sleep"):
            self.assertEqual(youtube_upload._next_chunk_with_retry(upload), ("status", {"id": "video"}))
        self.assertEqual(upload.calls, 2)

    def test_resumable_upload_does_not_retry_permanent_http_errors(self):
        from googleapiclient.errors import HttpError

        class Upload:
            def next_chunk(self):
                raise HttpError(SimpleNamespace(status=400, reason="bad request"), b"bad request")

        with self.assertRaises(HttpError):
            youtube_upload._next_chunk_with_retry(Upload())

    def test_upload_snippet_accepts_configured_category_and_language(self):
        class FakeUpload:
            def next_chunk(self):
                return None, {"id": "uploaded"}

        class Videos:
            body = None

            def insert(self, **kwargs):
                self.body = kwargs["body"]
                return FakeUpload()

        class Service:
            def __init__(self):
                self.video_resource = Videos()

            def videos(self):
                return self.video_resource

        service = Service()
        fake_media = lambda *args, **kwargs: (args, kwargs)
        fake_google = (None, None, None, lambda *_args, **_kwargs: service, fake_media)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "film.mp4"
            path.write_bytes(b"mp4")
            with patch.object(youtube_upload, "_google_modules", return_value=fake_google), \
                    patch.object(youtube_upload, "connect", return_value=object()):
                self.assertEqual(youtube_upload.upload_private_draft(
                    path, Path("client.json"), Path("token.json"), "Film", "Description", [],
                    category_id="22", default_language="hi",
                ), "uploaded")
        self.assertEqual(service.video_resource.body["snippet"]["categoryId"], "22")
        self.assertEqual(service.video_resource.body["snippet"]["defaultLanguage"], "hi")

    def test_edge_draft_preview_explicitly_allows_edge_synthesis(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)

            def synthesize(text, out_path, engine, voice, speed, **kwargs):
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(b"preview")
                return tts.TTSResult(out_path, 1.0, engine, voice)

            with patch.object(tts.config, "CACHE_DIR", cache_dir), \
                    patch.object(tts, "synthesize", side_effect=synthesize) as synth:
                data = tts.sample("en-US-AndrewMultilingualNeural", engine="edge")
            self.assertEqual(data, b"preview")
            self.assertTrue(synth.call_args.kwargs["allow_edge_draft"])

    def test_ai_video_fallback_uses_the_project_setting(self):
        project = jobs.Project({
            "id": "ai-video-fallback-test",
            "settings": {**jobs.DEFAULT_SETTINGS, "ai_video_fallback_source": "ai_image"},
            "scene_state": {},
            "logs": [],
        })
        scene = planner.Scene(id="ai-video", source="ai_video", title="Hero shot")
        image = Path("fallback.jpg")
        messages = []
        with patch.object(jobs, "ensure_image", return_value=image) as ensure_image:
            kind, result = jobs.ensure_visual(project, scene, messages.append, lambda: False)
        self.assertEqual((kind, result), ("image", image))
        ensure_image.assert_called_once_with(project, scene, messages.append)
        self.assertIn("configured ai_image fallback", messages[0])

    def test_existing_project_receives_new_settings_without_losing_its_values(self):
        project_id = "phase0-migration-test"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project_dir = root / project_id
            project_dir.mkdir()
            (project_dir / "project.json").write_text(json.dumps({
                "id": project_id,
                "settings": {"channel_name": "Legacy channel"},
                "scene_state": {},
                "logs": [],
            }), encoding="utf-8")
            with patch.object(jobs, "STUDIO_ROOT", root):
                loaded = jobs.load_project(project_id)
                self.assertEqual(loaded.settings["channel_name"], "Legacy channel")
                self.assertEqual(loaded.settings["ai_video_fallback_source"], "original_motion")
                self.assertEqual(loaded.settings["youtube_category_id"], "27")
            with jobs._OPEN_LOCK:
                jobs._OPEN.pop(project_id, None)


if __name__ == "__main__":
    unittest.main()
