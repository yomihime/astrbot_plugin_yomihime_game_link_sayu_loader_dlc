import asyncio
from collections import Counter
from hashlib import sha256
from io import BytesIO
import json
import os
from time import monotonic
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import Mock, patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
from plugin_downloads import Downloads  # noqa: E402
from port_errors import Unsupported  # noqa: E402
from resource_demand import ResourcePath, ResourceReads, install_read_audit  # noqa: E402
import resource_demand  # noqa: E402


def listing(entries):
    return (
        "<pre>"
        + "".join(f'<a href="{n}">{n}</a> time {s}\n' for n, s in entries)
        + "</pre>"
    ).encode()


class CheckedMirrorConnections(TestCase):
    def test_reuse_requires_same_host_current_public_peer_and_complete_response(self):
        import socket
        from http.client import IncompleteRead
        import plugin_public_http as public

        host = "mirror.example.org"
        url = f"https://{host}/manifest.json"

        def response(body, missing=0):
            reply = Mock(status=200, length=missing, will_close=False)
            reply.getheader.return_value = "identity"
            reply.getheaders.return_value = []
            reply.read1.side_effect = [body, b""]
            return reply

        conn = Mock(host=host)
        conn.sock.getpeername.return_value = ("8.8.8.8", 443)
        conn.getresponse.side_effect = [
            response(b"a"),
            response(b"b"),
            response(b"", missing=1),
        ]
        pool = public._IdleConnections()
        dns = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 443))]
        with (
            patch.object(public, "_mirror_connections", pool),
            patch.object(public.socket, "getaddrinfo", return_value=dns) as resolve,
            patch.object(public, "_open_checked", return_value=conn) as opening,
        ):
            self.assertEqual(
                public.read_public_https(url, 16, accept="*/*", reuse_connection=True)[
                    2
                ],
                b"a",
            )
            self.assertIsNone(pool.take("other.example.org", []))
            resolve.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 443))
            ]
            with self.assertRaises(Unsupported):
                public.read_public_https(url, 16, accept="*/*", reuse_connection=True)
            resolve.return_value = dns
            self.assertEqual(
                public.read_public_https(url, 16, accept="*/*", reuse_connection=True)[
                    2
                ],
                b"b",
            )
            with self.assertRaises(IncompleteRead):
                public.read_public_https(url, 16, accept="*/*", reuse_connection=True)
            self.assertEqual(opening.call_count, 1)
            self.assertEqual(resolve.call_count, 4)
            self.assertEqual(pool.idle, [])
            conn.close.assert_called_once()


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
        self.addCleanup(self.reads.join)
        self.addCleanup(self.reads.close)
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
            ("timeout", TimeoutError),
            ("server_error", OSError),
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
                    "server_error": (503, [], b""),
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

    async def test_complete_multi_manifest_cache_and_changed_declaration(self):
        self.reads.hash_route = "hash"
        await self.declare({"wiki/art": self.local, "hash": self.local.parent / "hash"})
        checks = self.base + "/Fixture/hash/"
        first, second = b"{}", b"[]"
        self.wire[self.index] = listing([("a.json", 2), ("b.json", 2), ("new.json", 2)])
        self.wire[self.index + "a.json"] = first
        self.wire[self.index + "b.json"] = self.wire[self.index + "new.json"] = second
        manifests = [{"publisher_metadata": "revision-text"} for _ in range(17)]
        manifests[0]["wiki/art/a.json"] = sha256(first).hexdigest()
        manifests[-1]["wiki/art/b.json"] = sha256(second).hexdigest()

        def publish():
            files = []
            for i, manifest in enumerate(manifests):
                name = f"part-{i}.json"
                raw = json.dumps(manifest).encode()
                files.append((name, len(raw)))
                self.wire[checks + name] = raw
            self.wire[checks] = listing(files)

        publish()
        for name, value in (("a.json", first), ("b.json", second)):
            with self.reads.scope():
                self.assertEqual((self.local / name).read_bytes(), value)
        self.assertEqual(
            sum(n for url, n in self.calls.items() if url.startswith(checks)), 18
        )
        self.assertEqual(self.reads.counts["checksum_set_cache_hits"], 1)
        # A newly published name is absent from the cached snapshot. Its new
        # digest is checked before it can be considered an uncovered item.
        manifests[-1]["wiki/art/new.json"] = sha256(first).hexdigest()
        publish()
        with (
            self.reads.scope(),
            self.assertRaisesRegex(Unsupported, "incorrect published checksum"),
        ):
            (self.local / "new.json").read_bytes()
        self.assertFalse(Path(self.local / "new.json").exists())

    async def test_conflicts_and_collection_limits_cannot_publish_files(self):
        self.reads.hash_route = "hash"
        await self.declare({"wiki/art": self.local, "hash": self.local.parent / "hash"})
        checks = self.base + "/Fixture/hash/"
        self.wire[self.index] = listing([("a.json", 2)])
        self.wire[self.index + "a.json"] = b"{}"
        raw = (
            '{"wiki/art/a.json":"'
            + "0" * 64
            + '","wiki/art/a.json":"'
            + "1" * 64
            + '"}'
        ).encode()
        self.wire[checks + "one.json"] = raw
        cases = [
            (listing([("one.json", len(raw))]), "conflicting"),
            (listing([(f"item-{i}.json", 2) for i in range(65)]), "collection exceeds"),
            (
                listing([(f"item-{i}.json", 4 * 1024 * 1024) for i in range(3)]),
                "collection exceeds",
            ),
            (listing([("one.json", 2), ("one.json", 2)]), "duplicate"),
        ]
        for index, message in cases:
            self.wire[checks] = index
            with (
                self.subTest(message=message),
                self.reads.scope(),
                self.assertRaisesRegex(Unsupported, message),
            ):
                (self.local / "a.json").read_bytes()
            self.assertFalse(Path(self.local / "a.json").is_file())
            self.assertFalse(self.reads.checksums)

    async def test_declared_background_set_is_shared_and_outlives_a_slow_read(self):
        self.reads.hash_route = "hash"
        checks = self.base + "/Fixture/hash/"
        doc = json.dumps({"wiki/art/first.json": sha256(b"{}").hexdigest()}).encode()
        gate, entered = Event(), Event()

        def delayed():
            entered.set()
            if not gate.wait(3):
                raise AssertionError("test did not release manifest")
            return doc

        self.wire = {
            checks: listing([("one.json", len(doc))]),
            checks + "one.json": delayed,
            self.index: listing([("first.json", 2)]),
            self.index + "first.json": b"{}",
        }
        await self.declare({"wiki/art": self.local, "hash": self.local.parent / "hash"})
        self.assertFalse(self.calls)  # Actual declaration alone cannot delay ready.
        self.reads.start()
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            self.reads.start()
            with self.reads.scope():
                resource_demand._scope.get().end = monotonic() + 0.05
                with self.assertRaisesRegex(TimeoutError, "still loading"):
                    (self.local / "first.json").read_bytes()
            self.assertFalse(Path(self.local / "first.json").is_file())
            self.assertEqual(
                self.reads.diagnostics()["checksum_sets"][0]["state"], "loading"
            )
        finally:
            gate.set()
        await asyncio.to_thread(self.reads.metadata_jobs[checks].result, 2)
        with self.reads.scope():
            self.assertEqual((self.local / "first.json").read_bytes(), b"{}")
        self.assertEqual(self.calls[checks + "one.json"], 1)
        self.assertEqual(self.calls[checks], 1)
        self.assertEqual(self.reads.counts["checksum_set_joined"], 1)

    async def test_prefetch_requires_actual_same_origin_and_preserves_hard_failure(
        self,
    ):
        self.reads.hash_route = "hash"
        checks = self.base + "/Fixture/hash/"
        self.reads.start()
        await self.declare()
        self.assertFalse(self.reads.metadata_jobs)
        self.wire = {
            checks: (403, [], b""),
            self.index: listing([("one.json", 2)]),
            self.index + "one.json": b"{}",
        }
        await self.declare({"hash": self.local.parent / "hash"})
        future = self.reads.metadata_jobs[checks]
        with self.assertRaises(PermissionError):
            await asyncio.to_thread(future.result, 2)
        with self.reads.scope(), self.assertRaises(PermissionError):
            (self.local / "one.json").exists()
        self.assertEqual(self.calls[checks], 1)
        self.assertEqual(
            self.reads.diagnostics()["checksum_sets"][0]["state"], "PermissionError"
        )
        self.assertFalse(Path(self.local / "one.json").is_file())

    async def test_verification_never_uses_another_declared_origin(self):
        alternate = "https://another.example.org"
        self.reads.hash_route = "hash"
        self.port.mirror.bases += (alternate,)
        await self.declare()

        async def no_bulk_data(url, limit):
            return 404, b""

        with patch.object(self.port, "_read", no_bulk_data):
            await self.port.mirror.download_all_file(
                "Fixture", {"hash": self.local.parent / "hash"}, URL=alternate
            )
        self.wire = {
            self.index: listing([("one.json", 2)]),
            self.index + "one.json": b"{}",
            alternate + "/Fixture/hash/": listing([]),
        }
        self.reads.start()
        await asyncio.to_thread(
            self.reads.metadata_jobs[alternate + "/Fixture/hash/"].result, 2
        )
        with self.reads.scope(), self.assertRaisesRegex(Unsupported, "for this mirror"):
            (self.local / "one.json").read_bytes()
        self.assertFalse(Path(self.local / "one.json").is_file())
        self.assertNotIn(self.base + "/Fixture/hash/", self.calls)

    async def test_retire_discards_inflight_checksum_result(self):
        self.reads.hash_route = "hash"
        checks = self.base + "/Fixture/hash/"
        entered, release = Event(), Event()

        def delayed():
            entered.set()
            if not release.wait(3):
                raise AssertionError("test did not release checksum request")
            return b"{}"

        self.wire = {checks: listing([("one.json", 2)]), checks + "one.json": delayed}
        await self.declare({"hash": self.local.parent / "hash"})
        self.reads.start()
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            self.reads.close()
        finally:
            release.set()
        await asyncio.to_thread(self.reads.join)
        self.assertFalse(self.reads.checksums)
        self.assertEqual(
            self.reads.diagnostics()["checksum_sets"][0]["state"], "retired"
        )
