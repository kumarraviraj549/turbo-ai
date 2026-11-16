import unittest

import numpy as np

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


if __name__ == "__main__":
    unittest.main()
