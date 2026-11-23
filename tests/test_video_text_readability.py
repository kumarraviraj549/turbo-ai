from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import ImageFont

from tubeforge import render


class VideoTextReadabilityTests(unittest.TestCase):
    def test_minimal_captions_have_readable_render_size(self):
        image, _, _ = render.render_caption_png(["Readable captions"], "Minimal", 1920, 1080)
        self.assertGreaterEqual(image.height, 150)

    def test_end_card_has_a_large_subscribe_button_without_branding(self):
        width, height = 1920, 1080
        image = np.asarray(render.render_end_card_png("A Film", width, height))
        red_button = (image[:, :, 0] == 230) & (image[:, :, 1] == 33) & (image[:, :, 2] == 23)
        ys, xs = np.where(red_button)
        self.assertGreater(xs.size, 0)
        self.assertGreaterEqual(xs.max() - xs.min() + 1, 450)
        self.assertGreaterEqual(ys.max() - ys.min() + 1, 95)

    def test_hindi_caption_renders_with_a_devanagari_font_for_pillow_and_ass(self):
        text = "भारत की कहानी"
        image, _, _ = render.render_caption_png([text], "Minimal", 1280, 720)
        self.assertTrue(np.any(np.asarray(image)[:, :, 3] > 0))
        font = render.load_font(48, text)
        self.assertTrue(any(name in str(getattr(font, "path", ""))
                            for name in ("Nirmala", "NotoSansDevanagari")))
        expected_layout = ImageFont.Layout.RAQM if render.pillow_raqm_available() else ImageFont.Layout.BASIC
        self.assertEqual(font.layout_engine, expected_layout)
        with tempfile.TemporaryDirectory() as temp_dir:
            ass = render.write_kinetic_ass(
                [{"word": text, "start": 0.0, "end": 1.0}], Path(temp_dir) / "hindi.ass", 1280, 720
            )
            content = ass.read_text(encoding="utf-8")
        family = "Nirmala UI" if render.sys.platform == "win32" else "Noto Sans Devanagari"
        self.assertIn(f"Style: Kinetic,{family},", content)

    @unittest.skipUnless(render.pillow_raqm_available(), "Pillow must be built with libraqm.")
    def test_hindi_font_uses_raqm_shaping(self):
        font = render.load_font(48, "भारत की कहानी")
        self.assertEqual(font.layout_engine, ImageFont.Layout.RAQM)

    def test_hindi_caption_falls_back_to_libass_when_pillow_lacks_raqm(self):
        with patch.object(render, "pillow_raqm_available", return_value=False):
            image, _, _ = render.render_caption_png(["भारत की कहानी"], "Minimal", 1280, 720)
        self.assertTrue(np.any(np.asarray(image)[:, :, 3] > 0))


if __name__ == "__main__":
    unittest.main()
