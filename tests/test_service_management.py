"""Fail closed when service declarations require unavailable permissions or inputs."""

import asyncio
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
import sayu_compat as host
from declarations import Unsupported
from service_settings import management_scope


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        host.install("Example", "ex", self.root, runtime_mode="package")

    async def test_unsupported_decorators_and_options_leave_no_route(self):
        service = host.SV("limited")

        async def handler(bot, event):
            self.fail("unsupported registration executed")

        for kwargs in ({"to_me": True}, {"to_ai": "tool"}, {"unknown": False}):
            with self.subTest(kwargs=kwargs), self.assertRaises(Unsupported):
                service.on_fullmatch("run", **kwargs)(handler)
        with self.assertRaises(Unsupported):
            service.on_file("file")(handler)
        self.assertEqual(host.registered_triggers(), [])
        with self.assertRaises(Unsupported):
            host.SV("limited", unknown=True)

    async def test_enabling_cannot_grant_permission(self):
        plugin = host.Plugins("Example", pm=1, force_prefix=["ex"])
        service = host.SV("reader")

        @service.on_command("read")
        async def blocked(bot, event):
            self.fail("permission check bypassed")

        with management_scope(writable=True):
            plugin.set(enabled=True)
            service.disable()
            service.enable()
            with self.assertRaises(NotImplementedError):
                plugin.set(pm=6)
            with self.assertRaises(NotImplementedError):
                service.set(enabled=False, white_list=[])
        self.assertTrue(service.enabled)
        with self.assertRaises(Unsupported):
            host.SV("reader", white_list=["someone"])
        with self.assertRaises(Unsupported):
            host.Plugins("Example", pm=6)
        with self.assertRaises(host.NoMatch):
            await host.dispatch("exread", conversation_kind="direct")

    async def test_readonly_and_background_tasks_cannot_manage(self):
        service = host.SV("probe")
        release = asyncio.Event()

        async def delayed():
            await release.wait()
            service.disable()

        with management_scope(writable=True):
            stale = asyncio.create_task(delayed())
        with management_scope():
            with self.assertRaises(NotImplementedError):
                service.disable()
            with self.assertRaises(NotImplementedError):
                with management_scope(writable=True):
                    service.disable()
            chat = asyncio.create_task(delayed())
            with self.assertRaises(NotImplementedError):
                await asyncio.to_thread(service.disable)
        release.set()
        for task in (stale, chat):
            with self.assertRaises(NotImplementedError):
                await task
        self.assertTrue(service.enabled)
        self.assertFalse((self.root / "services.sqlite3").exists())

    async def test_declarations_and_stale_objects_cannot_write_settings(self):
        previous = host.SV("reader")
        host.install("Other", "ot", self.root / "other", runtime_mode="declarations")
        with self.assertRaises(Unsupported):
            previous.enable()
        denied = host.SV("reader", white_list=["other"])
        with self.assertRaises(Unsupported):
            denied.set(enabled=True)
        with self.assertRaises(Unsupported):
            denied.on_fullmatch("read")(lambda *_: None)
        self.assertFalse((self.root / "other" / "services.sqlite3").exists())
