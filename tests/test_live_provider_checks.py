import unittest
import tempfile
import io
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from scripts.test_live_providers import _result
from tubeforge.http import ProviderError
from tubeforge import cache, images, youtube_upload
from scripts.test_live_providers import _cloudflare


class LiveProviderCheckTests(unittest.TestCase):
    def test_success_and_unconfigured_statuses(self):
        success = _result("mock", lambda: "ok")
        skipped = _result("mock", lambda: "must not run", configured=False)
        self.assertEqual(success["status"], "PASS")
        self.assertEqual(success["http_status"], 200)
        self.assertEqual(skipped["status"], "SKIPPED")

    def test_provider_http_failures_are_classified_without_details(self):
        for code, expected in ((400, "FAIL"), (401, "FAIL"), (404, "FAIL"), (429, "RATE_LIMITED")):
            with self.subTest(code=code):
                result = _result("mock", lambda: (_ for _ in ()).throw(
                    ProviderError("mock", "sensitive provider response", status=code)
                ))
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["http_status"], code)
                self.assertNotIn("sensitive provider response", result["message"])

    def test_live_cloudflare_check_does_not_double_count_reserved_neurons(self):
        image_buffer = io.BytesIO()
        Image.new("RGB", (1024, 1024), "blue").save(image_buffer, format="PNG")
        with patch.object(cache, "reserve_cloudflare_neurons", return_value=True) as reserve, \
                patch.object(cache, "record_usage") as record, \
                patch.object(images, "_cf_flux1_schnell", return_value=image_buffer.getvalue()):
            detail = _cloudflare()

        self.assertIn("1024x1024", detail)
        reserve.assert_called_once()
        record.assert_called_once_with("cloudflare")

    def test_youtube_upload_is_resumable_and_forced_private(self):
        class UploadRequest:
            def next_chunk(self):
                return None, {"id": "video-id"}

        class Endpoint:
            def __init__(self):
                self.kwargs = None

            def insert(self, **kwargs):
                self.kwargs = kwargs
                return self if kwargs.get("part") == "snippet" else UploadRequest()

            def set(self, **kwargs):
                self.kwargs = kwargs
                return self

            def execute(self):
                return {"id": "ok"}

        class Service:
            def __init__(self):
                self.video_endpoint = Endpoint()
                self.thumbnail_endpoint = Endpoint()
                self.caption_endpoint = Endpoint()

            def videos(self):
                return self.video_endpoint

            def thumbnails(self):
                return self.thumbnail_endpoint

            def captions(self):
                return self.caption_endpoint

        service = Service()
        media_calls = []
        build_api = lambda *args, **kwargs: service

        def media(path, **kwargs):
            media_calls.append((path, kwargs))
            return object()

        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "video.mp4"
            video.write_bytes(b"mp4")
            thumb = Path(directory) / "thumb.jpg"
            Image.new("RGB", (8, 8), "blue").save(thumb, format="JPEG")
            captions = Path(directory) / "captions.srt"
            captions.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
            with patch.object(youtube_upload, "_google_modules",
                              return_value=(None, None, None, build_api, media)), \
                    patch.object(youtube_upload, "connect", return_value=object()):
                video_id = youtube_upload.upload_private_draft(
                    video, Path(directory) / "client.json", Path(directory) / "token.json",
                    "Title", "Description", ["tag"], thumb, captions,
                )
        self.assertEqual(video_id, "video-id")
        self.assertEqual(service.video_endpoint.kwargs["body"]["status"]["privacyStatus"], "private")
        self.assertEqual(media_calls[0][1]["chunksize"], 8 * 1024 * 1024)
        self.assertTrue(media_calls[0][1]["resumable"])
        self.assertEqual(media_calls[1][1]["mimetype"], "image/jpeg")
        self.assertEqual(len(media_calls), 3)
        self.assertEqual(service.caption_endpoint.kwargs["body"]["snippet"]["language"], "en")


if __name__ == "__main__":
    unittest.main()
