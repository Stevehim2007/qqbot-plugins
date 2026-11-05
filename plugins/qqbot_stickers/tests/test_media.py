import unittest

from media import (
    ImageValidationError,
    decode_base64_payload,
    detect_image_magic,
    image_dimensions,
    inspect_image_bytes,
)
from tests.image_factory import avif, bmp, gif, jpeg, png, webp_vp8l, webp_vp8x


class MediaTests(unittest.TestCase):
    def test_png_magic_and_dimensions(self):
        self.assertEqual(detect_image_magic(png()), ("image/png", ".png"))
        self.assertEqual(image_dimensions(png(31, 7), "image/png"), (31, 7))

    def test_gif_magic_and_dimensions(self):
        self.assertEqual(detect_image_magic(gif()), ("image/gif", ".gif"))
        self.assertEqual(image_dimensions(gif(9, 13), "image/gif"), (9, 13))

    def test_jpeg_magic_and_dimensions(self):
        self.assertEqual(detect_image_magic(jpeg()), ("image/jpeg", ".jpg"))
        self.assertEqual(image_dimensions(jpeg(17, 11), "image/jpeg"), (17, 11))

    def test_bmp_magic_and_dimensions(self):
        self.assertEqual(detect_image_magic(bmp()), ("image/bmp", ".bmp"))
        self.assertEqual(image_dimensions(bmp(12, 14), "image/bmp"), (12, 14))

    def test_webp_magic_and_vp8x_dimensions(self):
        data = webp_vp8x(22, 33)
        self.assertEqual(detect_image_magic(data), ("image/webp", ".webp"))
        self.assertEqual(image_dimensions(data, "image/webp"), (22, 33))

    def test_webp_lossless_dimensions(self):
        self.assertEqual(
            image_dimensions(webp_vp8l(23, 34), "image/webp"), (23, 34)
        )

    def test_avif_magic_and_dimensions(self):
        data = avif(40, 41)
        self.assertEqual(detect_image_magic(data), ("image/avif", ".avif"))
        self.assertEqual(image_dimensions(data, "image/avif"), (40, 41))

    def test_invalid_magic_rejected(self):
        self.assertIsNone(detect_image_magic(b"not an image"))
        with self.assertRaises(ImageValidationError) as caught:
            inspect_image_bytes(b"not an image", max_bytes=100)
        self.assertEqual(caught.exception.code, "invalid_image")

    def test_oversize_rejected(self):
        with self.assertRaises(ImageValidationError) as caught:
            inspect_image_bytes(png(), max_bytes=10)
        self.assertEqual(caught.exception.code, "too_large")

    def test_dimension_bound_rejected(self):
        with self.assertRaises(ImageValidationError) as caught:
            inspect_image_bytes(png(500, 10), max_bytes=1000, max_dimension=100)
        self.assertEqual(caught.exception.code, "invalid_dimensions")

    def test_pixel_bound_rejected(self):
        with self.assertRaises(ImageValidationError) as caught:
            inspect_image_bytes(
                png(100, 100), max_bytes=1000, max_dimension=200, max_pixels=100
            )
        self.assertEqual(caught.exception.code, "invalid_dimensions")

    def test_minimum_dimension_rejected(self):
        with self.assertRaises(ImageValidationError):
            inspect_image_bytes(png(1, 1), max_bytes=1000, min_dimension=2)

    def test_accepts_data_uri_base64(self):
        import base64
        raw = base64.b64encode(png()).decode("ascii")
        self.assertEqual(decode_base64_payload("data:image/png;base64," + raw), png())

    def test_rejects_bad_base64(self):
        self.assertIsNone(decode_base64_payload("data:image/png;base64,%%%"))

    def test_inspect_returns_bounded_info(self):
        info = inspect_image_bytes(png(10, 20), max_bytes=1000)
        self.assertEqual((info.width, info.height, info.size, info.extension), (10, 20, len(png(10, 20)), ".png"))


if __name__ == "__main__":
    unittest.main()
