import asyncio
from collections import Counter
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
from plugin_downloads import Downloads  # noqa: E402
from port_errors import Unsupported  # noqa: E402
from resource_demand import ResourcePath, ResourceReads, install_read_audit  # noqa: E402


def listing(entries):
    return (
        "<pre>"
        + "".join(f'<a href="{n}">{n}</a> time {s}\n' for n, s in entries)
        + "</pre>"
    ).encode()


class DemandTest(IsolatedAsyncioTestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        self.base = "https://fixture.example.org"
        self.index = self.base + "/Fixture/wiki/art/"
        self.port = Downloads(root, plugin="Fixture", bulk_mirrors=(self.base,))
        self.local = ResourcePath(root / "resources/Fixture/published")
        self.profile = SimpleNamespace(
            PACKAGE="Fixture", STATIC_RESOURCE_DIRECTORIES=("wiki/art", "hash")
        )
        self.wire = {}
        self.calls = Counter()
        self.reads = ResourceReads(
            self.port, self.profile, SimpleNamespace(read_before=self.read)
        )
        self.port.mirror.demand = self.reads
        install_read_audit()

    def read(self, url, limit, deadline):
        self.calls[url] += 1
        result = self.wire[url]
        if callable(result):
            result = result()
        if isinstance(result, Exception):
            raise result
        return result if isinstance(result, tuple) else (200, [], result)

    async def declare(self, folders=None):
        async def unavailable(url, limit):
            return 404, b""

        with patch.object(self.port, "_read", unavailable):
            await self.port.mirror.download_all_file(
                "Fixture", folders or {"wiki/art": self.local}, URL=self.base
            )

    async def test_real_read_and_path_probe_verify_index_without_exposing_local_names(
        self,
    ):
        path = self.local / "region" / "card.png"
        out = BytesIO()
        Image.new("RGB", (3, 4), "blue").save(out, "PNG")
        data = out.getvalue()
        self.wire = {
            self.index: listing([("region/", "-"), ("table.json", 2)]),
            self.index + "region/": listing([("card.png", len(data))]),
            self.index + "region/card.png": data,
            self.index + "table.json": b"{}",
        }
        with self.reads.scope():
            self.assertFalse(path.is_file())
        self.assertFalse(self.calls)
        await self.declare()
        with self.assertRaises(FileNotFoundError):
            open(path, "rb")
        with self.reads.scope():
            with self.assertRaises(FileNotFoundError):
                open(self.local / "user-private" / "never-send.png", "rb")
            with self.assertRaises(FileNotFoundError):
                open(self.local.parent / "players" / "personal.json", "rb")
            self.assertFalse((self.local / "absent.png").exists())
            self.assertFalse((self.local / "bin.exe").exists())
            with Image.open(str(path)) as img:
                self.assertEqual(img.size, (3, 4))
            self.assertTrue((self.local / "table.json").is_file())
            self.assertEqual((self.local / "table.json").read_bytes(), b"{}")
            self.assertEqual(path.read_bytes(), data)
        self.assertEqual(self.calls, Counter({url: 1 for url in self.wire}))
        self.assertEqual(self.reads.counts["unlisted"], 2)
        self.assertEqual(self.reads.counts["published"], 2)
        self.assertFalse((self.local / "user-private").exists())

    async def test_write_modes_existing_files_and_dispatch_scope(self):
        await self.declare()
        with self.reads.scope():
            with open(self.local / "user.json", "wb") as stream:
                stream.write(b"private")
            with open(self.local / "user.json", "r+b") as stream:
                self.assertEqual(stream.read(), b"private")
            self.assertTrue((self.local / "user.json").exists())
            if hasattr(os, "O_TEMPORARY"):
                with self.assertRaises(FileNotFoundError):
                    os.open(
                        self.local / "delete-on-close.png", os.O_RDONLY | os.O_TEMPORARY
                    )
        self.assertFalse(self.calls)
        self.assertFalse((self.local / "outside-scope.json").is_file())
        self.assertFalse(self.calls)

    async def test_inflight_bulk_cannot_overwrite_an_on_demand_publication(self):
        arrived, release = asyncio.Event(), asyncio.Event()
        target = self.local / "shared.json"
        index = listing([("shared.json", 2)])
        self.wire = {self.index: index, self.index + "shared.json": b"{}"}

        async def slow_bulk(url, limit):
            if url.endswith("/"):
                return 200, index
            arrived.set()
            await release.wait()
            return 200, b"[]"

        with patch.object(self.port, "_read", slow_bulk):
            bulk = asyncio.create_task(
                self.port.mirror.download_all_file(
                    "Fixture", {"wiki/art": self.local}, URL=self.base
                )
            )
            await asyncio.wait_for(arrived.wait(), 2)
            try:
                with self.reads.scope():
                    self.assertEqual(target.read_bytes(), b"{}")
            finally:
                release.set()
                await bulk
        self.assertEqual(target.read_bytes(), b"{}")
        self.assertEqual(self.port.mirror.counts["unchanged"], 1)

    async def test_ordinary_missing_hard_denials_sizes_and_hashes(self):
        await self.declare({"wiki/art": self.local, "hash": self.local.parent / "hash"})
        self.reads.hash_route = "hash"
        manifest_url = self.base + "/Fixture/hash/"
        payload = b"{}"
        for state, wanted in (
            ("missing", False),
            ("denied", PermissionError),
            ("short", Unsupported),
            ("timeout", False),
            ("bad_hash", Unsupported),
            ("good", True),
        ):
            with self.subTest(state=state):
                name = state + ".json"
                self.reads.indexes.clear()
                self.wire[self.index] = listing([(name, 2)])
                self.wire[self.index + name] = {
                    "missing": (404, [], b""),
                    "denied": (403, [], b""),
                    "short": b"{",
                    "timeout": TimeoutError(),
                    "bad_hash": payload,
                    "good": payload,
                }[state]
                digest = sha256(payload).hexdigest() if state == "good" else "0" * 64
                checks = json.dumps({"wiki/art/" + name: digest}).encode()
                self.wire[manifest_url] = listing([("published.json", len(checks))])
                self.wire[manifest_url + "published.json"] = checks
                with self.reads.scope():
                    if isinstance(wanted, type):
                        with self.assertRaises(wanted):
                            (self.local / name).is_file()
                    else:
                        self.assertIs((self.local / name).exists(), wanted)
                self.assertEqual(Path(self.local / name).is_file(), state == "good")

    async def test_racing_read_does_not_overwrite_a_published_file_and_can_retire(self):
        await self.declare()
        for retire in (False, True):
            name = "retire.json" if retire else "race.json"
            self.reads.indexes.clear()
            arrived, release = Event(), Event()

            def delayed():
                arrived.set()
                if not release.wait(3):
                    raise AssertionError("test did not release reader")
                return b"{}"

            self.wire[self.index] = listing([(name, 2)])
            self.wire[self.index + name] = delayed
            target = self.local / name
            with self.reads.scope():
                reading = asyncio.create_task(asyncio.to_thread(target.exists))
                self.assertTrue(await asyncio.to_thread(arrived.wait, 2))
                if retire:
                    self.reads.close()
                else:
                    target.write_bytes(b"winner")
                release.set()
                self.assertIs(await reading, not retire)
            if retire:
                self.assertFalse(target.is_file())
            else:
                self.assertEqual(target.read_bytes(), b"winner")

    async def test_unlisted_hash_is_allowed_only_after_all_manifests_were_read(self):
        self.reads.hash_route = "hash"
        await self.declare({"wiki/art": self.local, "hash": self.local.parent / "hash"})
        checks = self.base + "/Fixture/hash/"
        self.wire = {
            self.index: listing([("plain.json", 2)]),
            self.index + "plain.json": b"{}",
            checks: listing([("a.json", 2), ("b.json", 2)]),
            checks + "a.json": b"{}",
            checks + "b.json": (404, [], b""),
        }
        with self.reads.scope(), self.assertRaises(Unsupported):
            (self.local / "plain.json").is_file()
        self.assertFalse(Path(self.local / "plain.json").exists())
        self.wire[checks + "b.json"] = b"{}"
        with self.reads.scope():
            self.assertTrue((self.local / "plain.json").is_file())
        self.assertEqual(Path(self.local / "plain.json").read_bytes(), b"{}")
