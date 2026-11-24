import sys
import subprocess
import tempfile
from io import BytesIO
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
from plugin_downloads import Downloads
from port_errors import Unsupported


class StaticDownloadsTest(IsolatedAsyncioTestCase):
    def setUp(self):
        import httpx2

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.hits = []
        buf = BytesIO()
        Image.new("RGB", (5, 3), "#452615").save(buf, "PNG")
        self.raw = buf.getvalue()

        def respond(request):
            self.hits.append(str(request.url))
            if request.url.path.endswith("/missing.png"):
                return httpx2.Response(404)
            if request.url.path.endswith("/redirect.png"):
                return httpx2.Response(
                    302, headers={"location": "https://elsewhere.invalid/"}
                )
            return httpx2.Response(200, content=self.raw)

        self.port = Downloads(
            self.root,
            transport=httpx2.MockTransport(respond),
            image_origins=["https://enka.network"],
        )
        self.url = "https://enka.network/ui/fixture.png"

    async def test_image_download_and_existing_cache(self):
        original = await self.port.get_image(self.url, self.root)
        self.assertEqual(original.size, (5, 3))
        small = await self.port.get_image(self.url, self.root, (2, 2))
        self.assertEqual(small.size, (2, 2))
        self.assertEqual(self.hits, [self.url])
        self.assertEqual((self.root / "fixture.png").read_bytes(), self.raw)
        self.assertEqual(
            await self.port.download(self.url, self.root, "other.png"), 200
        )
        self.assertEqual((self.root / "other.png").read_bytes(), self.raw)

    async def test_non_success_keeps_prior_file(self):
        target = self.root / "cached.png"
        target.write_bytes(b"prior")
        for suffix, code in (("missing.png", 404), ("redirect.png", 302)):
            status = await self.port.download(
                "https://enka.network/ui/" + suffix, self.root, target.name
            )
            self.assertEqual(status, code)
            self.assertEqual(target.read_bytes(), b"prior")
        missing = await self.port.get_image(
            "https://enka.network/ui/missing.png", self.root
        )
        self.assertEqual(
            (missing.mode, missing.size, missing.getbbox()), ("RGBA", (256, 256), None)
        )

    async def test_unavailable_permissions(self):
        for url in (
            "https://example.org/a.png",
            "https://enka.network@localhost/a.png",
            "http://enka.network/ui/a.png",
            "https://enka.network/ui/a.png?credential=fixture",
            "https://enka.network/%2e%2e/a.png",
        ):
            with self.assertRaises(Unsupported):
                await self.port.download(url, self.root, "a.png")
        for folder, name in (
            (self.root, "../a.png"),
            (self.root.parent, "a.png"),
            (self.root, "a.py"),
        ):
            with self.assertRaises(Unsupported):
                await self.port.download(self.url, folder, name)
        with self.assertRaises(Unsupported):
            await self.port.mirror.download_all_file("Fixture", {"icons": self.root})
        self.assertEqual(self.hits, [])

    async def test_default_origins_and_offline_failures(self):
        from httpx2 import MockTransport, Response
        from plugin_resource_mirror import DEFAULT_MIRRORS
        from port_errors import OfflineNetwork

        calls = []

        def controlled(request):
            calls.append((request.url.host, request.url.path))
            if request.url.host == DEFAULT_MIRRORS[-1].split("//")[1]:
                if request.url.path == "/":
                    return Response(200, content=b"Index of /")
                return Response(404)
            raise OfflineNetwork("offline fixture")

        port = Downloads(
            self.root, plugin="Fixture", transport=MockTransport(controlled)
        )
        folder = self.root / "assets"
        result = await port.mirror.download_all_file("Fixture", {"images": folder})
        self.assertIsNone(result)
        self.assertEqual(len(calls), len(DEFAULT_MIRRORS) + 1)
        self.assertTrue(folder.is_dir())
        self.assertFalse(list(folder.iterdir()))
        diag = port.mirror.diagnostics()
        self.assertEqual(diag["counts"], {"offline": 4, "http_error": 1})
        self.assertEqual(diag["bytes_written"], 0)
        self.assertEqual(diag["failures"][-1]["stage"], "index")

    async def test_mirror_does_not_swallow_permission_or_integrity(self):
        from httpx2 import MockTransport, Response
        import ssl

        base = "https://fixture.example.test"
        cases = {
            "bad_tls": ssl.SSLCertVerificationError("fixture"),
            "local_acl": PermissionError("fixture"),
            "http_forbidden": 403,
            "escape": "..%2Foutside.json",
            "program": "not-data.exe",
            "damaged": "data.json",
            "short": "image.png",
        }
        for case, adversarial in cases.items():
            with self.subTest(case=case):

                def send(request):
                    if isinstance(adversarial, Exception):
                        raise adversarial
                    if isinstance(adversarial, int):
                        return Response(adversarial)
                    if request.url.path.endswith("/"):
                        html = f'<pre><a href="{adversarial}">{adversarial}</a>  17:00 2\n</pre>'
                        return Response(200, content=html.encode())
                    return Response(200, content=b"!" if case == "short" else b"xx")

                port = Downloads(
                    self.root,
                    plugin="Fixture",
                    bulk_mirrors=(base,),
                    transport=MockTransport(send),
                )
                folder = self.root / case
                with self.assertRaises(
                    (
                        Unsupported,
                        PermissionError,
                        ssl.SSLCertVerificationError,
                        ValueError,
                    )
                ):
                    await port.mirror.download_all_file(
                        "Fixture", {"images": folder}, URL=base
                    )
                self.assertTrue(port.mirror.diagnostics()["failures"])
                self.assertFalse(list(folder.iterdir()))

    async def test_dns_resolved_to_local_address_is_not_contacted(self):
        from unittest.mock import patch
        from socket import AF_INET, SOCK_STREAM

        with (
            patch(
                "socket.getaddrinfo",
                return_value=[(AF_INET, SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
            ),
            patch("socket.socket") as sock,
        ):
            from plugin_public_http import read_public_https

            with self.assertRaises(Unsupported):
                read_public_https(
                    "https://approved.example.test/Fixture/", 128, accept="*/*"
                )
            sock.assert_not_called()

    async def test_offline_worker_uses_real_port_and_creates_empty_directories(self):
        worker = Path(__file__).resolve().parents[1] / "worker"
        subprocess.run(
            [
                sys.executable,
                "-IB",
                "-c",
                """
import asyncio, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from plugin_downloads import Downloads
from plugin_resource_mirror import DEFAULT_MIRRORS
from worker_storage import install_offline_guard

async def observe():
    root = Path.cwd()
    port = Downloads(root, plugin="Fixture")
    install_offline_guard()
    result = await port.mirror.download_all_file("Fixture", {"icons": root / "a", "art": root / "b"})
    evidence = port.mirror.diagnostics()
    assert result is None
    assert evidence["counts"] == {"offline": len(DEFAULT_MIRRORS), "no_available_mirror": 2}, evidence
    assert evidence["bytes_written"] == 0
    assert len(evidence["failures"]) == len(DEFAULT_MIRRORS) + 2
    assert all(folder.is_dir() and not list(folder.iterdir()) for folder in (root / "a", root / "b"))
asyncio.run(observe())
""",
                str(worker),
            ],
            cwd=self.root,
            check=True,
            timeout=25,
        )
