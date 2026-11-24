import base64
import sys
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))

from PIL import Image, ImageFont
from PIL.PngImagePlugin import PngInfo

import images as owner
from sayu_compat import UnsupportedMessage


def part(image, fmt="PNG", **options):
    stream = BytesIO()
    image.save(stream, format=fmt, **options)
    return {"image": base64.b64encode(stream.getvalue()).decode("ascii")}


def prepare(parts):
    return owner.prepare_owner_parts(parts, Path(__file__).parent, "fixture")


def public(parts):
    return owner.prepare_public_parts(parts, Path(__file__).parent, "fixture")


def result_image(parts):
    (result,) = prepare(parts)
    return Image.open(BytesIO(base64.b64decode(result["image"])))


class OwnerPartsTests(unittest.TestCase):
    def test_pure_text_preserves_multiple_parts(self):
        parts = [{"text": "first"}, {"text": "第二行\n"}]
        self.assertIs(prepare(parts), parts)

    def test_jpeg_and_metadata_free_transparent_png(self):
        with result_image([part(Image.new("RGB", (24, 32), "red"), "JPEG")]) as image:
            self.assertEqual(
                (image.format, image.mode, image.size), ("PNG", "RGB", (24, 32))
            )
            self.assertGreater(image.getpixel((0, 0))[0], 245)
        info = PngInfo()
        info.add_text("private_metadata", "unnecessary for display")
        rgba = (180, 40, 10, 120)
        with result_image(
            [part(Image.new("RGBA", (8, 2), rgba), pnginfo=info)]
        ) as image:
            self.assertEqual(image.getpixel((1, 1)), rgba)
            self.assertEqual(image.info, {})

    def test_images_only_preserve_order_and_alpha(self):
        parts = [
            part(Image.new("RGBA", (4, 2), (255, 0, 0, 75))),
            part(Image.new("RGB", (2, 3), "blue")),
        ]
        with result_image(parts) as image:
            self.assertEqual(image.size, (4, 5))
            self.assertEqual(image.getpixel((1, 1)), (255, 0, 0, 75))
            self.assertEqual(image.getpixel((1, 2)), (0, 0, 255, 255))

    def test_multiline_text_between_original_images(self):
        parts = [
            part(Image.new("RGB", (640, 3), "red")),
            {"text": "one\ntwo " * 2 + "a" * 160},
            part(Image.new("RGB", (640, 2), "blue")),
        ]
        with patch.object(
            owner, "worker_font", return_value=ImageFont.load_default(size=24)
        ):
            with result_image(parts) as image:
                self.assertEqual(image.getpixel((0, 2)), (255, 0, 0))
                self.assertEqual(image.getpixel((0, image.height - 2)), (0, 0, 255))
                self.assertGreater(image.height, 3 + 4 * 24 + 2)
                text_block = image.crop((0, 3, image.width, image.height - 2))
                self.assertEqual(text_block.convert("L").getextrema(), (0, 255))

    def test_cjk_worker_font(self):
        try:
            face = owner.worker_font(Path(__file__).parent, "fixture", 24)
        except UnsupportedMessage:
            self.skipTest("no CJK system font in test environment")
        self.assertNotEqual(bytes(face.getmask("星")), bytes(face.getmask("海")))
        with result_image(
            [{"text": "星海"}, part(Image.new("RGB", (1, 1), "red"))]
        ) as image:
            self.assertEqual(
                image.crop((0, 0, image.width, image.height - 1))
                .convert("L")
                .getextrema(),
                (0, 255),
            )

    def test_transport_limits_and_invalid_inputs(self):
        pixels = part(Image.new("RGB", (70, 10), "red"))
        tiny = part(Image.new("RGB", (1, 1)))
        with patch.object(owner, "MAX_INPUT_PIXELS", 1024):
            with self.assertRaises(UnsupportedMessage):
                prepare([pixels, pixels])
        with patch.object(owner, "MAX_BYTES", 16):
            with self.assertRaises(UnsupportedMessage):
                prepare([pixels])
        with patch.object(owner, "MAX_HEIGHT", 1):
            with self.assertRaises(UnsupportedMessage):
                prepare([tiny, tiny])
        for invalid in ({"image": "?"}, {"audio": "unsupported"}):
            with self.assertRaises(UnsupportedMessage):
                prepare([invalid])

    def test_dimension_fit_preserves_entire_images(self):
        halves = [part(Image.new("RGB", (256, 64), color)) for color in ("red", "blue")]
        with patch.object(owner, "MAX_WIDTH", 160):
            with result_image(halves) as image:
                self.assertEqual(image.size, (160, 80))
                self.assertEqual(image.getpixel((0, 0)), (255, 0, 0))
                self.assertEqual(image.getpixel((159, 79)), (0, 0, 255))
        with patch.object(owner, "MAX_PIXELS", 13000):
            with result_image([halves[0]]) as image:
                self.assertLessEqual(image.width * image.height, 13000)
                self.assertAlmostEqual(image.width / image.height, 4, delta=0.1)
        with (
            patch.object(owner, "MAX_WIDTH", 40),
            self.assertRaises(UnsupportedMessage),
        ):
            prepare(halves)

    def test_public_and_owner_have_independent_byte_budgets(self):
        from random import Random

        noise = Image.frombytes("RGB", (180, 160), Random(25).randbytes(180 * 160 * 3))
        parts = [{"text": "before"}, part(noise), {"text": "after"}]
        with patch.object(owner, "PUBLIC_BYTES", 45_000):
            result = public(parts)
            self.assertEqual((result[0], result[-1]), (parts[0], parts[-1]))
            self.assertLessEqual(len(base64.b64decode(result[1]["image"])), 45_000)
            private = prepare([parts[1]])
            self.assertGreater(len(base64.b64decode(private[0]["image"])), 45_000)
        with patch.object(owner, "MAX_BYTES", 45_000):
            with result_image([parts[1]]) as image:
                self.assertLess(image.width, noise.width)
                self.assertGreaterEqual(image.width, noise.width // 2)
        with self.assertRaises(UnsupportedMessage):
            public([{"image": base64.b64encode(b"bad image").decode()}])

    def test_public_alpha_over_budget_keeps_visible_and_transparent_pixels(self):
        from random import Random

        raw = Image.frombytes("RGB", (160, 120), Random(1).randbytes(160 * 120 * 3))
        frame = raw.convert("RGBA")
        frame.paste((0, 0, 0, 0), (0, 0, 30, 30))
        with patch.object(owner, "PUBLIC_BYTES", 23_000):
            (result,) = public([part(frame)])
        encoded = base64.b64decode(result["image"])
        self.assertLessEqual(len(encoded), 23_000)
        image = Image.open(BytesIO(encoded)).convert("RGBA")
        self.assertEqual(image.size, frame.size)
        self.assertEqual(image.getpixel((0, 0))[3], 0)
        self.assertEqual(image.getpixel((80, 60))[3], 255)

    def test_animated_png_fails_without_sending_first_frame(self):
        first = Image.new("RGB", (3, 3), "red")
        second = Image.new("RGB", (3, 3), "blue")
        animated = part(first, save_all=True, append_images=[second], duration=30)
        with self.assertRaises(UnsupportedMessage):
            prepare([animated])

    def test_public_multi_image_chain_is_complete_or_rejected(self):
        parts = [
            part(Image.new("RGB", (320, 40), "red")),
            {"text": "between images"},
            part(Image.new("RGB", (320, 60), "blue")),
        ]
        with patch.object(
            owner, "worker_font", return_value=ImageFont.load_default(size=24)
        ):
            (result,) = public(parts)
        with Image.open(BytesIO(base64.b64decode(result["image"]))) as image:
            self.assertEqual(image.getpixel((320, 0)), (255, 0, 0))
            self.assertEqual(image.getpixel((320, image.height - 1)), (0, 0, 255))
            self.assertEqual(
                image.crop((0, 40, 640, image.height - 60)).convert("L").getextrema(),
                (0, 255),
            )
        tall = part(Image.new("RGB", (320, 641), "red"))
        with self.assertRaises(UnsupportedMessage):
            public([tall, tall])
        with self.assertRaises(UnsupportedMessage):
            public([parts[0], {"image": "bad"}])
