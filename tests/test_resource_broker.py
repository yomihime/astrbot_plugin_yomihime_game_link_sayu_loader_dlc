import io
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import patch
from zipfile import ZipFile
from hashlib import sha256

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "worker")]
from port_errors import Unsupported  # noqa: E402
from resource_broker import MAX_FILE, MAX_INDEX, allowed, fetch  # noqa: E402
from resource_policy import from_snapshot  # noqa: E402
import resource_broker  # noqa: E402
from port_errors import StartupTransferLimit  # noqa: E402


class BrokerTest(TestCase):
    profile = SimpleNamespace(
        PACKAGE="Fixture",
        STATIC_RESOURCE_URLS=("https://exact.example.org/logo.png",),
        PUBLIC_IMAGE_ORIGINS=("https://cdn.example.org",),
        PUBLIC_BULK_MIRRORS=("https://mirror.example.org",),
        STATIC_RESOURCE_DIRECTORIES=("wiki/art",),
    )

    def test_policy_restricts_origin_path_kind_and_byte_limit(self):
        self.assertEqual(
            allowed("https://cdn.example.org/ui/f.png", self.profile),
            ("image", MAX_FILE),
        )
        self.assertEqual(
            allowed("https://mirror.example.org/Fixture/wiki/art/sub/", self.profile),
            ("index", MAX_INDEX),
        )
        self.assertEqual(
            allowed("https://mirror.example.org/Fixture/wiki/art/a.json", self.profile),
            ("json", MAX_FILE),
        )
        for url in (
            "https://mirror.example.org/Other/wiki/art/f.png",
            "https://mirror.example.org/Fixture/wiki/artful/data.json",
            "https://mirror.example.org/Fixture/wiki/art/a.exe",
            "https://mirror.example.org/Fixture/wiki/art/%2E%2E/hidden.png",
            "https://mirror.example.org/Fixture/wiki/art/%252e%252e/f.png",
            "https://exact.example.org/unlisted.png",
            "https://cdn.example.org/data.json",
            "https://cdn.example.org/a.png?token=fixture",
            "https://cdn.example.org@localhost/x.png",
            "http://cdn.example.org/ok.png",
            "https://foreign.example.org/a.png",
        ):
            with (
                self.subTest(url=url),
                self.assertRaises(Unsupported),
                patch(
                    "socket.getaddrinfo", side_effect=AssertionError("DNS attempted")
                ),
            ):
                fetch(url, MAX_FILE, self.profile)

    def test_validates_bytes_and_strips_ambient_headers(self):
        output = io.BytesIO()
        Image.new("RGB", (2, 3), "#EA8721").save(output, "PNG")
        url = "https://exact.example.org/logo.png"
        with patch(
            "plugin_public_http.read_public_https",
            return_value=(200, [("Set-Cookie", "fixture")], output.getvalue()),
        ) as read:
            self.assertEqual(
                fetch(url, 512, self.profile),
                (200, [("Content-Type", "image/png")], output.getvalue()),
            )
            read.assert_called_once_with(
                url, 512, accept="image/*", skip_error_body=True, error_body_limit=8192
            )
        with (
            patch(
                "plugin_public_http.read_public_https",
                return_value=(200, [], b"not an image"),
            ),
            self.assertRaises(Unsupported),
        ):
            fetch(url, 512, self.profile)
        with patch(
            "plugin_public_http.read_public_https",
            return_value=(302, [("Location", "https://elsewhere.invalid")], b"private"),
        ):
            self.assertEqual(fetch(url, 512, self.profile), (302, [], b""))

    def test_source_manifest_uses_the_pinned_archive_and_actual_download_call(self):
        with TemporaryDirectory() as tmp:
            archive = Path(tmp) / "plugin.zip"
            with ZipFile(archive, "w") as z:
                z.writestr(
                    "Fixture/plugin.py",
                    "from gsuid_core.utils.download_resource.download_core import download_all_file as load\n"
                    "icon='https://publisher.example.org/a.png'\n"
                    "async def fetch():\n"
                    " mapping={'wiki/art':ART, f'build/{PLATFORM}':BIN}\n"
                    " other={'ignored/dir': DATA}\n"
                    " await load('Fixture', mapping)\n",
                )
            lock = {
                "package": "Fixture",
                "snapshot_sha256": sha256(archive.read_bytes()).hexdigest(),
            }
            self.assertEqual(
                from_snapshot(archive, lock),
                (["https://publisher.example.org/a.png"], ["wiki/art"]),
            )
            with self.assertRaises(ValueError):
                from_snapshot(archive, dict(lock, snapshot_sha256="0" * 64))

    def test_isolated_reader_serves_static_bytes_while_plugin_network_is_denied(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "worker.pyz"
            output = io.BytesIO()
            Image.new("RGB", (2, 3), "#A22731").save(output, "PNG")
            sample = output.getvalue()
            # The deterministic fixture replaces only the broker's final HTTPS
            # read. Real process isolation, immutable URL policy, frame parsing and
            # the plugin process audit hook all execute through their normal paths.
            with ZipFile(package, "w") as archive:
                for name in (
                    "resource_broker.py",
                    "port_errors.py",
                    "plugin_resource_mirror.py",
                ):
                    archive.write(ROOT / "worker" / name, name)
                archive.writestr(
                    "game_profile.py",
                    "PUBLIC_RESOURCES_ONLY=True\nPACKAGE='Fixture'\nSTATIC_RESOURCE_URLS=('https://fixed.example.org/a.png',)\n"
                    "STATIC_RESOURCE_DIRECTORIES=PUBLIC_BULK_MIRRORS=PUBLIC_IMAGE_ORIGINS=()\n",
                )
                archive.writestr(
                    "plugin_public_http.py",
                    f"def read_public_https(url, limit, **kw):\n assert url == 'https://fixed.example.org/a.png'\n return 200, [], {sample!r}\n",
                )

            script = """
import socket, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from port_errors import OfflineNetwork, Unsupported
import resource_broker
from plugin_public_http import read_public_https
from worker_storage import install_offline_guard, install_write_guard
with resource_broker.activate(Path(sys.argv[2])) as _:
    helper = resource_broker.CLIENT.process
    install_write_guard(Path.cwd(), Path.cwd())
    install_offline_guard()
    for operation in (lambda: socket.getaddrinfo("fixed.example.org", 443),
                      lambda: socket.create_connection(("192.0.2.1", 443))):
        try: operation()
        except OfflineNetwork: pass
        else: raise AssertionError("direct network access permitted")
    try: read_public_https("https://invalid.example.org/no.png", 1000, accept="image/*")
    except Unsupported: pass
    else: raise AssertionError("unknown origin permitted")
    status, _, data = read_public_https("https://fixed.example.org/a.png", 1000, accept="image/*")
    assert status == 200 and len(data) == int(sys.argv[3])
assert helper.poll() is not None
"""
            subprocess.run(
                [
                    sys.executable,
                    "-IB",
                    "-c",
                    script,
                    str(ROOT / "worker"),
                    str(package),
                    str(len(sample)),
                ],
                cwd=root,
                check=True,
                timeout=15,
            )

    def test_concurrent_windows_charge_elapsed_time_once(self):
        clock = [100.0]
        budget = resource_broker.StartupBudget(3)
        with patch.object(resource_broker, "monotonic", side_effect=lambda: clock[0]):
            with budget.transfer() as first:
                clock[0] += 1
                with budget.transfer() as nested:
                    self.assertEqual(first, nested)
                    clock[0] += 1
            self.assertEqual(budget.remaining, 1)
            clock[0] += 20  # Import or other non-network work between transfers.
            with budget.transfer() as last:
                self.assertEqual(last, 123)
                clock[0] += 1
            with self.assertRaises(StartupTransferLimit), budget.transfer():
                self.fail("exhausted startup admitted another request")
        self.assertTrue(budget.diagnostics()["exhausted"])


class StartupMirrorTest(IsolatedAsyncioTestCase):
    async def test_partial_downloads_remain_visible_after_startup(self):
        import httpx2
        from plugin_downloads import Downloads

        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            reader = SimpleNamespace(budget=None)

            def respond(request):
                if not request.url.path.endswith("/"):
                    return httpx2.Response(200, content=b"{}")
                if reader.budget is not None and "/second/" in request.url.path:
                    raise StartupTransferLimit("test transfer expired")
                return httpx2.Response(
                    200, content=b'<pre><a href="a.json">a.json</a>  01:00 2\n</pre>'
                )

            base = "https://mirror.example.org"
            port = Downloads(
                root,
                transport=httpx2.MockTransport(respond),
                plugin="Fixture",
                bulk_mirrors=[base],
            )
            dirs = {name: root / name for name in ("first", "second", "third")}
            with patch.object(resource_broker, "CLIENT", reader):
                with resource_broker.startup_resources():
                    self.assertIsNone(
                        await port.mirror.download_all_file("Fixture", dirs, URL=base)
                    )
                self.assertIsNone(reader.budget)
                counts = port.mirror.diagnostics()["counts"]
                self.assertEqual(counts["startup_incomplete"], 2)
                self.assertEqual(counts["downloaded"], 1)
                self.assertEqual((root / "first/a.json").read_bytes(), b"{}")
                self.assertFalse((root / "second/a.json").exists())
                self.assertIsNone(
                    await port.mirror.download_all_file("Fixture", dirs, URL=base)
                )
                self.assertEqual(port.mirror.diagnostics()["counts"]["downloaded"], 3)
