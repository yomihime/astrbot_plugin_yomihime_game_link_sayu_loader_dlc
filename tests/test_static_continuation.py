import asyncio
from collections import Counter
from io import BytesIO
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from PIL import Image
import httpx2
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
from plugin_downloads import Downloads  # noqa: E402
from plugin_public_api import public_get  # noqa: E402
from port_errors import StartupTransferLimit, Unsupported  # noqa: E402
import resource_broker  # noqa: E402
from resource_httpx import static_httpx  # noqa: E402


class StaticTransport(IsolatedAsyncioTestCase):
    async def test_upstream_httpx_observes_anonymous_static_get_and_real_404(self):
        url = "https://cdn.example.org/official/icon.png"
        profile = SimpleNamespace(
            PACKAGE="Fixture",
            STATIC_RESOURCE_URLS=(url,),
            PUBLIC_IMAGE_ORIGINS=("https://cdn.example.org",),
            STATIC_RESOURCE_DIRECTORIES=(),
            PUBLIC_BULK_MIRRORS=(),
        )
        reader = SimpleNamespace(
            read=lambda uri, limit: resource_broker.fetch(uri, limit, profile)
        )
        response = (
            404,
            [("Content-Type", "text/plain; charset=utf-8"), ("Set-Cookie", "fixture")],
            b"file not found",
        )
        with (
            static_httpx(profile, reader),
            patch(
                "plugin_public_http.read_public_https", return_value=response
            ) as wire,
        ):
            async with httpx.AsyncClient() as client:
                result = await client.get(url)
                self.assertEqual(
                    (result.status_code, result.content), (404, b"file not found")
                )
                self.assertEqual(result.headers["content-type"], "text/plain")
                self.assertNotIn("set-cookie", result.headers)
                for target, options in (
                    ("https://cdn.example.org/other.png", {}),
                    (url, {"params": {"actor": 1}}),
                    (url, {"headers": {"Cookie": "session=fixture"}}),
                    (url, {"headers": {"Authorization": "Bearer fixture"}}),
                    (url, {"headers": {"X-Account": "fixture"}}),
                    ("http://127.0.0.1/file.png", {}),
                ):
                    with (
                        self.subTest(target=target, keys=list(options)),
                        self.assertRaises(Unsupported),
                    ):
                        await client.get(target, **options)
                with self.assertRaises(Unsupported):
                    await client.post(url)
                with self.assertRaises(Unsupported):
                    await client.request("GET", url, content=b"unexpected")
            for options in (
                {"verify": False},
                {"auth": ("sample", "test")},
                {"proxy": "http://127.0.0.1:8080"},
            ):
                async with httpx.AsyncClient(**options) as client:
                    with self.assertRaises(Unsupported):
                        await client.get(url)
        self.assertEqual(wire.call_count, 1)
        self.assertEqual(
            [(item["status"], item["bytes"]) for item in reader.static_httpx],
            [(404, 14)],
        )

    async def test_only_ambr_static_declarations_use_reader(self):
        profile = SimpleNamespace(
            PACKAGE="GenshinUID",
            STATIC_RESOURCE_URLS=(),
            PUBLIC_IMAGE_ORIGINS=(),
            PUBLIC_BULK_MIRRORS=(),
            STATIC_RESOURCE_DIRECTORIES=(),
        )
        icon = BytesIO()
        Image.new("RGB", (1, 2), "red").save(icon, "PNG")
        origin = "https://gi.yatta.moe"
        calls = []

        def static_response(url, limit, *, accept, skip_error_body):
            calls.append(url)
            return 200, [], icon.getvalue() if accept == "image/*" else b'{"events":[]}'

        reader = SimpleNamespace(
            read=lambda url, cap: resource_broker.fetch(url, cap, profile)
        )
        with (
            patch.object(resource_broker, "CLIENT", reader),
            patch("plugin_public_http.read_public_https", side_effect=static_response),
            patch(
                "httpx2.AsyncClient",
                side_effect=AssertionError("ordinary HTTP attempted"),
            ),
        ):
            for path in (
                "/assets/UI/UI_AvatarIcon_Fixture.png",
                "/assets/UI/monster/Example.png",
            ):
                result = await public_get(origin + path)
                self.assertEqual(result.content, icon.getvalue())
                self.assertEqual(result.headers["Content-Type"], "image/png")
            self.assertEqual(
                (await public_get(origin + "/assets/data/event.json")).json(),
                {"events": []},
            )
            for path in (
                "/api/v2/en/avatar",
                "/assets/data/other.json",
                "/assets/UI/monster/collect.exe",
                "/assets/UI/b/c/example.png",
                "/assets/UI/UI_test.png?key=sample",
                "/assets/UI/%2e%2e/a.png",
            ):
                with self.subTest(path=path), self.assertRaises(Unsupported):
                    await public_get(origin + path)
                with self.assertRaises(Unsupported):
                    resource_broker.allowed(origin + path, profile)
            with self.assertRaises(Unsupported):
                await public_get(origin + "/assets/data/event.json", params={"uid": 1})
            profile.PACKAGE = "Other"
            with self.assertRaises(Unsupported):
                await public_get(origin + "/assets/data/event.json")
        self.assertEqual(len(calls), 3)


class Continuation(IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.base = "https://mirror.example.org"
        self.reader = SimpleNamespace(budget=None)
        self.broker = patch.object(resource_broker, "CLIENT", self.reader)
        self.broker.start()
        self.addCleanup(self.broker.stop)
        self.hits = Counter()
        self.dirs = {"icons": self.root / "icons"}

    def mirror(self, transport):
        return Downloads(
            self.root,
            plugin="Fixture",
            bulk_mirrors=[self.base],
            transport=httpx2.MockTransport(transport),
        ).mirror

    async def startup(self, mirror):
        with resource_broker.startup_resources():
            await mirror.download_all_file("Fixture", self.dirs, URL=self.base)

    async def test_nested_cursor_resumes_only_unvisited_items(self):
        async def respond(req):
            p = req.url.path
            self.hits[p] += 1
            items = None
            if p.endswith("/icons/"):
                items = [("gone.json", "2"), ("sub/", "-"), ("last.json", "2")]
            elif p.endswith("/sub/"):
                items = [("done.json", "2"), ("cut.json", "2")]
            if items:
                html = (
                    "<pre>"
                    + "".join(
                        f'<a href="{name}">{name}</a> 12:00 {size}\n'
                        for name, size in items
                    )
                    + "</pre>"
                )
                return httpx2.Response(200, content=html.encode())
            if p.endswith("/gone.json"):
                return httpx2.Response(404)
            if p.endswith("/cut.json") and self.reader.budget:
                raise StartupTransferLimit("fixture timeout")
            return httpx2.Response(200, content=b"{}")

        mirror = self.mirror(respond)
        await self.startup(mirror)
        await self.startup(mirror)
        jobs = mirror.diagnostics()["continuations"]
        self.assertEqual(
            [(j["state"], j["pending_roots"]) for j in jobs], [("queued", 1)]
        )
        self.assertFalse((self.root / "icons/sub/cut.json").exists())
        mirror.start_continuations()
        mirror.start_continuations()
        await mirror._runner
        await mirror.stop_continuations()
        self.assertTrue(
            all(
                n == (2 if p.endswith("/cut.json") else 1) for p, n in self.hits.items()
            )
        )
        self.assertEqual((self.root / "icons/sub/cut.json").read_bytes(), b"{}")
        self.assertEqual((self.root / "icons/last.json").read_bytes(), b"{}")
        (job,) = mirror.diagnostics()["continuations"]
        self.assertEqual(
            (
                job["state"],
                job["failed_reads"],
                job["pending_roots"],
                job["published_bytes"],
            ),
            ("partial", 1, 0, 6),
        )

    async def test_retire_and_hard_errors_never_publish_or_retry(self):
        for outcome in ("cancel", "denied", "corrupt", "ordinary"):
            gate = asyncio.Event()

            async def respond(req):
                if req.url.path.endswith("/"):
                    return httpx2.Response(
                        200, content=b'<pre><a href="res.json">res</a> 0 2\n</pre>'
                    )
                if self.reader.budget and outcome != "ordinary":
                    raise StartupTransferLimit("fixture timeout")
                gate.set()
                if outcome == "cancel":
                    await asyncio.Event().wait()
                if outcome == "corrupt":
                    return httpx2.Response(200, content=b"x")
                return httpx2.Response(404 if outcome == "ordinary" else 403)

            mirror = self.mirror(respond)
            await self.startup(mirror)
            mirror.start_continuations()
            if outcome == "ordinary":
                self.assertEqual(mirror.diagnostics()["continuations"], [])
                continue
            await asyncio.wait_for(gate.wait(), timeout=2)
            if outcome != "cancel":
                await mirror._runner
            await mirror.stop_continuations()
            (job,) = mirror.diagnostics()["continuations"]
            self.assertEqual(
                job["state"], "cancelled" if outcome == "cancel" else "failed"
            )
            self.assertEqual(job["pending_roots"], 1)
            self.assertFalse((self.root / "icons/res.json").exists())

    async def test_generation_queue_is_bounded_and_reports_completed_roots(self):
        def respond(req):
            if self.reader.budget:
                raise StartupTransferLimit("fixture timeout")
            self.hits[req.url.path] += 1
            return httpx2.Response(200, content=b"<pre></pre>")

        mirror = self.mirror(respond)
        for index in range(5):
            self.dirs = {f"root{index}": self.root / f"root{index}"}
            await self.startup(mirror)
        self.assertEqual(mirror.diagnostics()["counts"]["continuation_capacity"], 1)
        mirror.start_continuations()
        await mirror._runner
        await mirror.stop_continuations()
        jobs = mirror.diagnostics()["continuations"]
        self.assertEqual([j["state"] for j in jobs], ["completed"] * 4)
        self.assertEqual(sum(j["pending_roots"] for j in jobs), 0)
        self.assertNotIn("/Fixture/root4/", self.hits)
