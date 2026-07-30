import unittest

from PIL import Image

from psg_converter import PSGConverter


class PSGConverterTest(unittest.TestCase):
    def test_opacity_scales_existing_alpha(self):
        image = Image.new("RGBA", (1, 1), (10, 20, 30, 200))

        result = PSGConverter()._scale_opacity(image, 0.5)

        self.assertEqual(result.getpixel((0, 0)), (10, 20, 30, 100))

    def test_opacity_is_clamped(self):
        transparent = Image.new("RGBA", (1, 1), (10, 20, 30, 200))
        opaque = Image.new("RGBA", (1, 1), (10, 20, 30, 200))

        PSGConverter()._scale_opacity(transparent, -1)
        PSGConverter()._scale_opacity(opaque, 2)

        self.assertEqual(transparent.getpixel((0, 0))[3], 0)
        self.assertEqual(opaque.getpixel((0, 0))[3], 200)


if __name__ == "__main__":
    unittest.main()
