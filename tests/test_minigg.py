import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
from plugin_minigg import MiniGG, MiniggNotFoundError, _write_cache


class MiniGGTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve() / "resources"
        self.port = MiniGG(self.root)

    async def test_cache_is_atomic_and_persists_across_instances(self):
        fetch = AsyncMock(
            return_value=(SimpleNamespace(status_code=200), b'{"name":"fixture"}')
        )
        ports = [MiniGG(self.root), self.port]
        with patch.object(MiniGG, "_get", fetch):
            results = await asyncio.gather(
                *[ports[i % 2].get_character_info("fixture") for i in range(16)]
            )
        self.assertEqual(results, [{"name": "fixture"}] * 16)
        fetch.assert_awaited_once()
        cache = self.root / "DATA_CACHE_PATH" / "get_character_info"
        self.assertEqual([p.name for p in cache.iterdir()], ["fixture.json"])
        self.assertEqual(json.loads((cache / "fixture.json").read_bytes()), results[0])
        new = MiniGG(self.root)
        with patch.object(new, "_get", side_effect=AssertionError("unexpected HTTP")):
            self.assertEqual(await new.get_character_info("fixture"), results[0])

    async def test_concurrent_publishers_and_open_readers_agree_on_one_entry(self):
        path = self.root / "entry.json"
        published = await asyncio.gather(
            *(asyncio.to_thread(_write_cache, path, {"value": i}) for i in range(16))
        )
        self.assertEqual(published, [published[0]] * 16)
        with path.open("rb") as reader:
            self.assertEqual(
                await asyncio.to_thread(_write_cache, path, {"value": -1}), published[0]
            )
            self.assertEqual(json.load(reader), published[0])
        self.assertEqual(list(self.root.iterdir()), [path])

    async def test_cache_misses_retry_transport_exceptions(self):
        with patch.object(
            self.port, "_get", new=AsyncMock(side_effect=OSError("unavailable"))
        ):
            with self.assertRaises(OSError):
                await self.port.get_talent_info("fixture")
        self.assertFalse(any(self.root.rglob("*.json")))
        with patch.object(
            self.port,
            "_get",
            new=AsyncMock(
                return_value=(SimpleNamespace(status_code=404), b'{"error":"missing"}')
            ),
        ):
            with self.assertRaises(MiniggNotFoundError) as error:
                await self.port.get_talent_info("fixture")
        self.assertEqual(error.exception.raw, {"error": "missing"})
        self.assertFalse(any(self.root.rglob("*.json")))

    async def test_confines_cache_and_transport(self):
        with patch.object(
            self.port, "_get", side_effect=AssertionError("unexpected HTTP")
        ):
            with self.assertRaises(ValueError):
                await self.port.get_character_info("test", cache_path=self.root.parent)
            for name in ("../other", "name:stream", "NUL", "some\\file"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    await self.port.get_character_info(name)
            for path in (
                "//other.test",
                "https://other.test/x",
                "/../external",
                "x?override=1",
            ):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    await self.port.minigg_request(path, "name")
