import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
import public_resources as resources


class PublicResourcesTest(unittest.TestCase):
    def test_only_a_declared_directory_and_numeric_png_are_materialized(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder).resolve()
            root = base / "resources"
            source = base / "source"
            declaration = source / "Example/utils/resource/files.py"
            declaration.parent.mkdir(parents=True)
            declaration.write_text(
                "from gsuid_core.utils.download_resource.download_core import download_all_file as download\n"
                'await download("Example", {"wiki/remote": ONE, "guide/two": TWO})',
                encoding="utf-8",
            )
            port = resources.PublicResources(source, "Example")
            port.bind(
                lambda module, name: root / "Example/wiki/one"
                if name == "ONE"
                else base / "outside",
                root,
            )
            fake = BytesIO()
            Image.new("RGB", (2, 2)).save(fake, format="PNG")
            with patch.object(
                resources, "_read_png", return_value=fake.getvalue()
            ) as fetch:
                valid = root / "Example/wiki/one/12345.png"
                self.assertTrue(port.ensure(root, valid))
                self.assertEqual(valid.read_bytes(), fake.getvalue())
                self.assertFalse(port.ensure(root, valid))
                for path in (
                    root / "Example/wiki/other/1.png",
                    root / "Example/wiki/one/custom.png",
                    root / "Example/wiki/one/2.py",
                    root / "other/wiki/one/2.png",
                    base / "elsewhere/1.png",
                    valid.parent / "../one/1.png",
                ):
                    self.assertFalse(port.ensure(root, path))
                fetch.assert_called_once_with(
                    resources.MIRROR + "/Example/wiki/remote/12345.png"
                )

    def test_broken_and_oversize_responses_are_rejected(self):
        class Response(BytesIO):
            status = 200
            headers = {}

            def geturl(self):
                return "https://example.invalid/image.png"

        for data, headers in (
            (b"bad png", {}),
            (b"short", {"Content-Length": "70000000"}),
        ):
            response = Response(data)
            response.headers = headers
            with patch.object(resources, "build_opener") as opener:
                opener.return_value.open.return_value = response
                with self.assertRaises((ValueError, OSError)):
                    resources._read_png(response.geturl())

    def test_redirect_never_requests_a_new_destination(self):
        self.assertIsNone(
            resources._NoRedirect().redirect_request(
                None, None, 302, "", {}, "https://example.invalid/"
            )
        )
