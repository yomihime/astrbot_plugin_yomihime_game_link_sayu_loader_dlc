"""Readable JPEG draft decoding and pagination use the same owner transport budgets."""

import base64
import sys
import unittest
from io import BytesIO
from math import ceil
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageChops, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
import images
from sayu_compat import UnsupportedMessage


def make_image(size=(799, 2601), *, fmt="JPEG", **kwargs):
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    for y in range(0, size[1], 87):
        draw.rectangle((0, y, size[0] - 1, y + 31), fill=(37, y % 255, 128))
    raw = BytesIO()
    image.save(raw, fmt, **kwargs)
    return raw.getvalue()


def part(raw):
    return {"image": base64.b64encode(raw).decode("ascii")}


def prepare(parts):
    return images.prepare_owner_parts(parts, Path(__file__).parent, "fixture")


def assemble(parts):
    (result,) = parts
    poster = result["poster"]
    frame = Image.new("RGB", (poster["width"], poster["height"]))
    columns = max(8, ceil(max(400, frame.width) / 96))
    for i, encoded in enumerate(poster["tiles"]):
        with Image.open(BytesIO(base64.b64decode(encoded, validate=True))) as tile:
            x, y = i % columns * tile.width, i // columns * tile.height
            frame.paste(tile, (x, y))
    return frame


class DraftPosterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.jpeg = make_image()
        with Image.open(BytesIO(cls.jpeg)) as image:
            image.draft("RGB", (399, 1300))
            cls.half = image.convert("RGB")

    @patch.object(images, "MAX_INPUT_PIXELS", 1_600_000)
    def test_odd_size_retains_entire_readable_raster(self):
        out = assemble(prepare([part(self.jpeg)]))
        self.assertEqual(out.size, (400, 1301))
        self.assertIsNone(ImageChops.difference(out, self.half).getbbox())

    @patch.object(images, "MAX_INPUT_PIXELS", 1_600_000)
    def test_captions_and_image_keep_order(self):
        face = ImageFont.load_default(size=24)
        captions = ["first", "last"]
        with patch.object(images, "worker_font", return_value=face):
            out = assemble(
                prepare([{"text": captions[0]}, part(self.jpeg), {"text": captions[1]}])
            )
        band = 32 + sum(face.getmetrics()) + 6
        self.assertEqual(out.size, (640, self.half.height + 2 * band))
        reference = Image.new("RGB", out.size, "white")
        draw = ImageDraw.Draw(reference)
        draw.text((16, 16), captions[0], fill="black", font=face, anchor="la")
        reference.paste(self.half, ((640 - self.half.width) // 2, band))
        draw.text(
            (16, 16 + band + self.half.height),
            captions[1],
            fill="black",
            font=face,
            anchor="la",
        )
        self.assertIsNone(ImageChops.difference(reference, out).getbbox())

    @patch.object(images, "MAX_INPUT_PIXELS", 1_600_000)
    def test_nonstreaming_and_incomplete_images_are_rejected(self):
        inputs = {
            "progressive": make_image(progressive=True),
            "truncated": self.jpeg[:-2],
            "multiple scans": self.jpeg[:-2] + b"\xff\xda" + self.jpeg[-2:],
            "trailing data": self.jpeg + b"tail",
            "oversized PNG": make_image(fmt="PNG"),
            "height": make_image((160, 18000)),
        }
        for name, raw in inputs.items():
            with self.subTest(name=name), self.assertRaises(UnsupportedMessage):
                prepare([part(raw)])

    def test_half_raster_must_fit_before_decode(self):
        with (
            patch.object(images, "MAX_INPUT_PIXELS", 500_000),
            patch.object(Image.Image, "convert") as convert,
        ):
            with self.assertRaises(UnsupportedMessage):
                prepare([part(self.jpeg)])
            convert.assert_not_called()

    def test_composite_budget_counts_decode_and_canvas(self):
        with (
            patch.object(images, "MAX_INPUT_PIXELS", 1_000_000),
            patch.object(
                images, "worker_font", return_value=ImageFont.load_default(size=24)
            ),
            patch.object(Image, "new") as canvas,
        ):
            with self.assertRaisesRegex(UnsupportedMessage, "composite pixel budget"):
                prepare([{"text": "caption"}, part(self.jpeg)])
            canvas.assert_not_called()

    @patch.object(images, "MAX_INPUT_PIXELS", 1_600_000)
    def test_oversized_member_of_multiple_image_chain_rejects_whole_chain(self):
        with self.assertRaises(UnsupportedMessage):
            prepare([{"text": "prefix"}, part(make_image((20, 20))), part(self.jpeg)])

    def test_input_and_lossless_tile_byte_budgets(self):
        with (
            patch.object(images, "MAX_INPUT_IMAGE_BYTES", 128),
            patch.object(images, "_draft_poster") as draft,
        ):
            with self.assertRaises(UnsupportedMessage):
                prepare([part(self.jpeg)])
            draft.assert_not_called()
        with (
            patch.object(images, "MAX_INPUT_PIXELS", 1_600_000),
            patch.object(images, "MAX_BYTES", 1500),
        ):
            with self.assertRaisesRegex(UnsupportedMessage, "worker transport"):
                prepare([part(self.jpeg)])

    @patch.object(images, "MAX_INPUT_PIXELS", 1_600_000)
    def test_public_large_source_still_rejected_and_normal_poster_unchanged(self):
        with self.assertRaises(UnsupportedMessage):
            images.prepare_public_parts([part(self.jpeg)], Path.cwd(), "fixture")
        regular = make_image((800, 1600))
        result = images.prepare_public_parts([part(regular)], Path.cwd(), "fixture")
        with Image.open(BytesIO(regular)) as source:
            self.assertIsNone(
                ImageChops.difference(source.convert("RGB"), assemble(result)).getbbox()
            )

    def test_marker_length_and_header_bound(self):
        self.assertFalse(images._single_scan_jpeg(b"\xff\xd8\xff\xc0\xff\xffshort"))
        self.assertFalse(
            images._single_scan_jpeg(
                self.jpeg[:2] + b"\xff\xfe\x00\x02" * 1024 + self.jpeg[2:]
            )
        )
