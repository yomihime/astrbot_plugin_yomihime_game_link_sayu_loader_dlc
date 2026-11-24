"""Exact directory listing permission is separate from a recursive asset mirror."""

from hashlib import sha256
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import urlsplit
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "worker")]

from port_errors import Unsupported  # noqa: E402
from resource_broker import MAX_INDEX, allowed, fetch  # noqa: E402
from resource_policy import from_snapshot  # noqa: E402


class PlatformIndexTests(TestCase):
    origin = "https://mirror.example.org"
    source = """from gsuid_core.utils.download_resource.download_core import download_all_file
def target_package():
    return 'test-3.12'
PLATFORM = target_package()
async def load():
    mapping = {'resource/known': OTHER, f'resource/build/{PLATFORM}/tools': HERE,
               f'resource/build/{user}/secret': THERE}
    unreferenced = {f'resource/{PLATFORM}/ignored': OTHER}
    await download_all_file('Fixture', mapping)
"""
    target = "win-amd64/cpython-312"
    declaration = "Fixture/resources.py"
    base = origin + "/Fixture/resource/build/test-3.12/tools/"

    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.archive = Path(tmp.name) / "snapshot.zip"
        with ZipFile(self.archive, "w") as archive:
            archive.writestr(self.declaration, self.source)
        self.lock = {
            "package": "Fixture",
            "snapshot_sha256": sha256(self.archive.read_bytes()).hexdigest(),
        }
        self.spec = {
            "declaration": self.declaration,
            "sha256": sha256(self.source.encode()).hexdigest(),
            "binding": "PLATFORM",
            "resolver": "target_package",
            "targets": {self.target: "test-3.12", "win-amd64/cpython-313": "test-3.13"},
        }
        indexes = {}
        self.policy = {"platform_indexes": self.spec, "bulk_mirrors": [self.origin]}
        _, directories = from_snapshot(
            self.archive, self.lock, platform_indexes=indexes, policy=self.policy
        )
        self.assertEqual(indexes[self.target], {self.base})
        self.assertEqual(directories, ["resource/known"])
        self.profile = SimpleNamespace(
            PACKAGE="Fixture",
            STATIC_RESOURCE_DIRECTORIES=directories,
            STATIC_RESOURCE_URLS=(),
            PUBLIC_IMAGE_ORIGINS=(),
            PUBLIC_BULK_MIRRORS=(self.origin,),
            STATIC_PLATFORM_INDEXES=indexes,
        )
        self.enterContext(
            patch("resource_broker.sysconfig.get_platform", return_value="win-amd64")
        )
        self.enterContext(
            patch(
                "resource_broker.sys.implementation",
                SimpleNamespace(cache_tag="cpython-312"),
            )
        )

    def test_declaration_and_resolver_must_match(self):
        for value in (
            {"sha256": "0" * 64},
            {"resolver": "unused"},
            {"declaration": "Fixture/other.py"},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                from_snapshot(
                    self.archive,
                    self.lock,
                    platform_indexes={},
                    policy=dict(self.policy, platform_indexes={**self.spec, **value}),
                )

    def test_transport_never_receives_undeclared_url(self):
        with patch("plugin_public_http.read_public_https") as read:
            for url in (
                self.base.removesuffix("/"),
                self.base + "sub/",
                self.base + "plugin.pyd",
                self.base + "archive.json",
                self.base + "cover.png",
                self.base + "?query",
                self.base.replace("tools/", "else/"),
                self.base.replace("test-3.12", "test-3.13"),
                self.base.replace("mirror.example.org", "other.example.org"),
                self.base.replace("https:", "http:"),
            ):
                with self.subTest(url=url), self.assertRaises(Unsupported):
                    fetch(url, MAX_INDEX, self.profile)
            read.assert_not_called()

    def test_read_is_bounded_and_listing_cannot_grant_files(self):
        path = urlsplit(self.base).path
        body = f'<html><title>Index of {path}</title><pre><a href="module.pyd">module.pyd</a> 01-Jan-2000 00:00 141</pre></html>'.encode()
        with patch(
            "plugin_public_http.read_public_https", return_value=(200, [], body)
        ) as read:
            self.assertEqual(
                allowed(self.base, self.profile), ("exact_index", MAX_INDEX)
            )
            self.assertEqual(
                fetch(self.base, 2 * MAX_INDEX, self.profile),
                (200, [("Content-Type", "text/html")], body),
            )
            read.assert_called_once_with(
                self.base, MAX_INDEX, accept="text/html", skip_error_body=True
            )
            with self.assertRaises(Unsupported):
                fetch(self.base + "module.pyd", 1024, self.profile)
            self.assertEqual(read.call_count, 1)

    def test_denied_missing_redirect_and_empty_are_not_successes(self):
        for status in (301, 302, 307, 403, 404):
            with (
                self.subTest(status=status),
                patch(
                    "plugin_public_http.read_public_https",
                    return_value=(
                        status,
                        [("Location", self.base + "sub/")],
                        b"denied",
                    ),
                ) as read,
            ):
                self.assertEqual(
                    fetch(self.base, MAX_INDEX, self.profile), (status, [], b"")
                )
                self.assertEqual(read.call_count, 1)
        for body in (
            b"",
            b"<pre>error</pre>",
            b"<title>Index of /wrong/</title><pre></pre>",
            f"<title>Index of {urlsplit(self.base).path}</title><pre>".encode(),
        ):
            with (
                self.subTest(body=body),
                patch(
                    "plugin_public_http.read_public_https", return_value=(200, [], body)
                ),
                self.assertRaises(Unsupported),
            ):
                fetch(self.base, MAX_INDEX, self.profile)
