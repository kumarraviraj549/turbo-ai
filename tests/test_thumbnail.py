import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

from tubeforge import render


class ThumbnailTests(unittest.TestCase):
    def test_hook_copy_and_variant_accent_change_thumbnail(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "scene.jpg"
            first = Path(tmp) / "first.jpg"
            second = Path(tmp) / "second.jpg"
            third = Path(tmp) / "third.jpg"
            image = Image.new("RGB", (640, 360), (28, 65, 110))
            ImageDraw.Draw(image).ellipse((360, 55, 560, 255), fill=(240, 150, 70))
            image.save(source)

            render.make_thumbnail(source, "A Guide to the Ocean", first, hook="What lives beneath the waves?",
                                  variant=0)
            render.make_thumbnail(source, "A Guide to the Ocean", second, hook="What lives beneath the waves?",
                                  variant=1)
            render.make_thumbnail(source, "A Guide to the Ocean", third, hook="Could the ocean hide a new world?",
                                  variant=0)

            with Image.open(first) as first_image, Image.open(second) as second_image, \
                    Image.open(third) as third_image:
                self.assertIsNotNone(ImageChops.difference(first_image, second_image).getbbox())
                self.assertIsNotNone(ImageChops.difference(first_image, third_image).getbbox())


if __name__ == "__main__":
    unittest.main()
