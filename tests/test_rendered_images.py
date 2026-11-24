import base64
from io import BytesIO
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
import images
import plugin_help
import rendered_images as drawing
import sayu_compat
from port_errors import Unsupported


class LocalCanvasesTest(TestCase):
    def setUp(self):
        drawing.clear_generated()
        self.addCleanup(drawing.clear_generated)

    def test_bounded_native_crop_keeps_pixels_and_rejects_large_intermediate(self):
        from plugin_image_tools import crop_center_img

        with Image.new("RGBA", (17, 9)) as source:
            source.putdata([(i % 229, i, 59, i) for i in range(153)])
            for size in ((7, 9), (4, 2), (80, 6)):
                native = crop_center_img(source, *size)
                self.assertEqual(
                    drawing.crop_canvas(source, *size).tobytes(), native.tobytes()
                )
            with patch.object(
                source, "resize", side_effect=AssertionError("allocated")
            ):
                with self.assertRaises(Unsupported):
                    drawing.crop_canvas(source, 12000, 1500)
                with self.assertRaises(Unsupported):
                    drawing.crop_canvas(source, 100, 12000)

    def test_cache_reopens_only_issued_bytes_above_input_budget(self):
        with (
            TemporaryDirectory() as tmp,
            patch.object(sayu_compat, "MAX_INPUT_PIXELS", 64),
        ):
            root = Path(tmp).resolve()
            with patch.object(plugin_help, "_assets", plugin_help._Assets(root, root)):
                target = root / "help.jpg"
                image = Image.new("RGB", (20, 15), "red")
                drawing.save_canvas(image, target, "JPEG", quality=85, subsampling=0)
                with drawing.open_cached_canvas(target) as cached:
                    self.assertEqual(cached.size, image.size)
                with self.assertRaises(Unsupported):
                    plugin_help.open_help_image(target)
                Image.new("RGB", image.size, "green").save(target)
                with self.assertRaises(Unsupported):
                    drawing.open_cached_canvas(target)

    def test_generated_encoding_and_decorations_stay_bounded(self):
        with Image.new("RGBA", (32, 46), "teal") as image:
            from plugin_image_tools import CustomizeImage

            self.assertEqual(
                drawing.canvas_color(image, True),
                CustomizeImage.get_bg_color(image, True),
            )
            with patch.object(sayu_compat, "MAX_INPUT_IMAGE_BYTES", 80):
                with self.assertRaisesRegex(Unsupported, "encoded canvas"):
                    drawing.encode_canvas(image)
            with patch.object(image, "resize", side_effect=AssertionError("allocated")):
                with self.assertRaises(Unsupported):
                    drawing.resize_decoration(image, (8192, 8192))

    def test_generated_output_uses_original_dimensions_for_owner_and_public(self):
        image = Image.new("RGB", (80, 64), "#ADFF2F")
        with (
            patch.object(sayu_compat, "MAX_INPUT_PIXELS", 64),
            patch.object(images, "MAX_INPUT_PIXELS", 64),
        ):
            with self.assertRaises(sayu_compat.UnsupportedMessage):
                sayu_compat.convert_img_sync(image)
            output = drawing.encode_canvas(image)
            stream = BytesIO()
            image.save(stream, "JPEG", quality=85)
            self.assertEqual(output, stream.getvalue())
            part = {"image": base64.b64encode(output).decode("ascii")}
            with patch.object(images, "MAX_PIXELS", 500):
                with self.assertRaisesRegex(
                    sayu_compat.UnsupportedMessage, "too small to read"
                ):
                    images.prepare_owner_parts([part], None, None)
            with patch.object(images, "PUBLIC_SIDE", 64):
                (public,) = images.prepare_public_parts([part], None, None)
                with Image.open(
                    BytesIO(base64.b64decode(public["image"]))
                ) as projected:
                    self.assertEqual(projected.size, (64, 51))
            # Re-encoding an unrelated image with the same shape must not inherit
            # permission to decode a locally rendered oversized image.
            other = BytesIO()
            Image.new("RGB", image.size, "blue").save(other, "JPEG", quality=85)
            forged = {"image": base64.b64encode(other.getvalue()).decode("ascii")}
            with self.assertRaisesRegex(
                sayu_compat.UnsupportedMessage, "source pixel limit"
            ):
                images.prepare_public_parts([forged], None, None)
            with self.assertRaisesRegex(
                sayu_compat.UnsupportedMessage, "source pixel limit"
            ):
                images.prepare_owner_parts([part, part], None, None)
