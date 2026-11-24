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


class BatchCompletion(IsolatedAsyncioTestCase):
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

    async def test_post_download_code_sees_terminal_files_and_explicit_retry(self):
        self.dirs.update({"more": self.root / "more"})

        async def respond(req):
            path = req.url.path
            self.hits[path] += 1
            if path.endswith("/"):
                return httpx2.Response(
                    200, content=b'<pre><a href="file.json">f</a> 2</pre>'
                )
            if path.endswith("/more/file.json") and self.reader.budget is not None:
                raise StartupTransferLimit("fixture timeout")
            return httpx2.Response(200, content=b"{}")

        mirror = self.mirror(respond)
        after = []
        try:
            await self.startup(mirror)
            after.append("success")
        except StartupTransferLimit:
            after.append("limited")
        snapshot = sorted(
            p.relative_to(self.root).as_posix() for p in self.root.rglob("*.json")
        )
        self.assertEqual((after, snapshot), (["limited"], ["icons/file.json"]))
        hits = self.hits.copy()
        await asyncio.sleep(0.05)
        self.assertEqual(self.hits, hits)
        self.assertFalse((self.root / "more/file.json").exists())
        self.assertEqual(
            mirror.diagnostics()["startup_outcomes"], {"startup_budget_exhausted": 1}
        )
        (first,) = mirror.diagnostics()["batches"]
        self.assertEqual(
            (first["state"], first["finished_roots"], first["pending_roots"]),
            ("limited", 1, 1),
        )
        await mirror.download_all_file("Fixture", self.dirs, URL=self.base)
        self.assertEqual(self.hits["/Fixture/icons/file.json"], 1)
        self.assertEqual((self.root / "more/file.json").read_bytes(), b"{}")
        self.assertEqual(
            mirror.diagnostics()["batch_outcomes"], {"limited": 1, "completed": 1}
        )

    async def test_soft_response_hard_failure_and_retirement(self):
        for outcome in ("cancel", "denied", "corrupt", "ordinary"):
            gate = asyncio.Event()

            async def respond(req):
                if req.url.path.endswith("/"):
                    return httpx2.Response(
                        200, content=b'<pre><a href="res.json">r</a> 2</pre>'
                    )
                gate.set()
                if outcome == "cancel":
                    await asyncio.Event().wait()
                if outcome == "corrupt":
                    return httpx2.Response(200, content=b"x")
                return httpx2.Response(404 if outcome == "ordinary" else 403)

            mirror = self.mirror(respond)
            job = asyncio.create_task(self.startup(mirror))
            await asyncio.wait_for(gate.wait(), timeout=2)
            if outcome == "cancel":
                job.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await job
                state = "cancelled"
            elif outcome == "ordinary":
                self.assertIsNone(await job)
                state = "soft_failures"
            else:
                with self.assertRaises((PermissionError, ValueError)):
                    await job
                state = "failed"
            (row,) = mirror.diagnostics()["batches"]
            self.assertEqual(
                (row["state"], row["pending_roots"]),
                (state, 0 if outcome == "ordinary" else 1),
            )
            self.assertFalse((self.root / "icons/res.json").exists())

    async def test_atomic_write_finishes_before_cancellation_returns(self):
        import threading

        entered, release = threading.Event(), threading.Event()

        async def respond(req):
            if req.url.path.endswith("/"):
                return httpx2.Response(
                    200, content=b'<pre><a href="ok.json">ok</a> 2</pre>'
                )
            return httpx2.Response(200, content=b"{}")

        mirror = self.mirror(respond)
        publish = mirror.port._write

        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise TimeoutError()
            return publish(*args, **kwargs)

        with patch.object(mirror.port, "_write", side_effect=delayed):
            task = asyncio.create_task(self.startup(mirror))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            task.cancel()
            await asyncio.sleep(0.02)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual((self.root / "icons/ok.json").read_bytes(), b"{}")
        row = mirror.diagnostics()["batches"][-1]
        self.assertEqual((row["state"], row["published_files"]), ("cancelled", 1))

    async def test_each_declared_root_has_its_own_page_and_children_remain_bounded(
        self,
    ):
        async def respond(req):
            if req.url.path.endswith("/wide/"):
                children = "".join(f'<a href="s{i}/">s</a> - ' for i in range(33))
                return httpx2.Response(200, content=f"<pre>{children}</pre>".encode())
            return httpx2.Response(200, content=b"<pre></pre>")

        mirror = self.mirror(respond)
        dirs = {f"root{i}": self.root / f"root{i}" for i in range(34)}
        await mirror.download_all_file("Fixture", dirs, URL=self.base)
        row = mirror.diagnostics()["batches"][-1]
        self.assertEqual(
            (row["state"], row["index_pages"], row["child_index_pages"]),
            ("completed", 34, 0),
        )
        dirs["wide"] = self.root / "wide"
        with self.assertRaises(Unsupported):
            await mirror.download_all_file("Fixture", dirs, URL=self.base)
        row = mirror.diagnostics()["batches"][-1]
        self.assertEqual(
            (row["state"], row["reason"], row["child_index_pages"]),
            ("limited", "directory_limit", 32),
        )
