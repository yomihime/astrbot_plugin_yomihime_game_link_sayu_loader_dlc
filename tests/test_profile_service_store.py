"""Configuration authority, migration recovery and live route state in one profile."""

import asyncio
from contextlib import closing
from contextvars import copy_context
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
import sayu_compat as loader
from port_errors import Unsupported
from service_settings import management_scope


class ProfileStoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.install()

    def install(self, name="probe", path=None):
        loader.install(name, "cp", path or self.root, runtime_mode="package")
        from gsuid_core.config import plugin_config_store
        from gsuid_core.sv import config_plugins

        self.store = plugin_config_store
        self.assertIs(self.store.get_all(), config_plugins)

    def test_routing_uses_the_active_projection_and_declared_role(self):
        plugin = loader.Plugins("probe", force_prefix=["cp"])
        service = loader.SV("test")
        restricted = loader.SV("admins", pm=0)

        @service.on_command("show")
        async def callback(bot, event):
            await bot.send("visible")

        @restricted.on_command("restricted")
        async def admin(bot, event):
            self.fail("profile settings elevated the caller")

        def dispatch(command):
            return asyncio.run(
                loader.dispatch(command, actor_id="actor", conversation_kind="direct")
            )

        with management_scope(writable=True):
            config = self.store.get("probe")
            config["sv"]["test"]["enabled"] = False
            self.assertFalse(service.enabled)
            with self.assertRaises(loader.NoMatch):
                dispatch("cpshow")
            service.set(priority=14, enabled=True)
            self.assertEqual(config["sv"]["test"]["priority"], 14)
            self.assertEqual(dispatch("cpshow"), [{"text": "visible"}])
            plugin.set(enabled=False, is_lazy=False)
            self.assertFalse(config["enabled"])
            self.store.set(
                "probe", {"enabled": True, "sv": {"admins": {"enabled": True}}}
            )
            self.assertTrue(plugin.enabled)
            with self.assertRaises(loader.NoMatch):
                dispatch("cprestricted")
            self.assertEqual(restricted.pm, 0)
            self.assertEqual(service.priority, 5)

    def test_unknown_or_foreign_fields_are_rejected_atomically(self):
        with management_scope(writable=True):
            self.store.set("probe", {"priority": 2, "sv": {"test": {"enabled": False}}})
            view = self.store.get("probe")
            for bad in [
                {"priority": 9, "pm": 0},
                {"unknown": 1},
                {"sv": {"test": {"enabled": True, "white_list": []}}},
            ]:
                with self.assertRaises(Unsupported):
                    view.update(bad)
                self.assertEqual(view["priority"], 2)
                self.assertFalse(view["sv"]["test"]["enabled"])
            for action in [
                lambda: self.store.get("foreign"),
                lambda: self.store.get_all()["foreign"],
                lambda: self.store.set("foreign", {}),
                lambda: self.store.save("foreign"),
                lambda: view.get("pm"),
                lambda: self.store.mark_dirty("foreign"),
            ]:
                with self.assertRaises(Unsupported):
                    action()
            with self.assertRaises(ValueError):
                self.store.set("probe", {"priority": True})
            self.assertIs(view, self.store.get("probe"))

    def test_retained_arguments_views_and_grants_cannot_write_after_expiry(self):
        arg = {"sv": {"test": {"enabled": True}}}
        with management_scope(writable=True):
            self.store.set("probe", arg)
            view = self.store.get_all()["probe"]["sv"]["test"]
            saved_grant = copy_context()
        arg["sv"]["test"]["enabled"] = False
        self.assertTrue(view["enabled"])
        for action in [
            lambda: view.update({"enabled": False}),
            view.clear,
            lambda: saved_grant.run(view.pop, "enabled"),
            self.store.save_all,
            self.store.save_dirty,
            lambda: self.store.mark_dirty("probe"),
        ]:
            with self.assertRaises(Unsupported):
                action()
        with management_scope(writable=True):
            old = self.store
            self.install("other", self.root / "other")
            with self.assertRaises(Unsupported):
                self.store.set("other", {})
            with self.assertRaises(Unsupported):
                old.get("probe")
            with self.assertRaises(Unsupported):
                view.get("enabled")
        with management_scope(writable=True):
            self.store.set("other", {"priority": 1})

    def test_prior_sqlite_rows_reopen_and_the_lazy_flag_controls_commit(self):
        path = self.root / "services.sqlite3"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE settings(kind,name,enabled,priority)")
            db.executemany(
                "INSERT INTO settings VALUES(?,?,?,?)",
                [
                    ("plugin", "probe", 1, 9),
                    ("service", "test", 0, 4),
                ],
            )
        self.install()
        query = loader.SV("test")
        self.assertFalse(query.enabled)
        self.assertEqual(query.priority, 4)
        with management_scope(writable=True):
            loader.flush_loaded_service_settings()
            query.set(priority=8)
        self.install()
        query = loader.SV("test")
        self.assertEqual(query.priority, 4)
        with management_scope(writable=True):
            query.set(priority=8, is_lazy=False)
        self.install()
        self.assertEqual(loader.SV("test").priority, 8)
        self.assertEqual(self.store.get("probe")["priority"], 9)

    def test_legacy_rejection_and_incomplete_cleanup_preserve_user_files(self):
        core = self.root / "config.json"
        for invalid in [
            {"other": {"priority": 4}},
            {"probe": {"enabled": False, "media": 1}},
        ]:
            raw = json.dumps({"plugins": invalid}).encode()
            core.write_bytes(raw)
            with self.assertRaises(Unsupported):
                self.install()
            self.assertEqual(core.read_bytes(), raw)
            self.assertFalse((self.root / "config_backup.json").exists())
        original = b'{"plugins":{"probe":{"priority":7}},"marker":"untouched"}'
        core.write_bytes(original)
        self.install()
        with management_scope(writable=True):
            with patch(
                "plugin_service_store.os.replace", side_effect=OSError("fixture abort")
            ):
                with self.assertRaises(OSError):
                    loader.flush_loaded_service_settings()
        self.assertEqual(core.read_bytes(), original)
        self.install()
        with management_scope(writable=True):
            loader.flush_loaded_service_settings()
        self.assertEqual((self.root / "config_backup.json").read_bytes(), original)
        self.assertEqual(json.loads(core.read_bytes()), {"marker": "untouched"})
        self.assertEqual(self.store.get("probe")["priority"], 7)

    def test_stale_database_change_cannot_overwrite_another_generation(self):
        with management_scope(writable=True):
            self.store.set("probe", {"priority": 3})
            self.store.save_all()
            self.store.set("probe", {"priority": 7})
            with closing(sqlite3.connect(self.root / "services.sqlite3")) as db, db:
                db.execute(
                    "UPDATE profile_service_config SET payload=?", ('{"priority": 5}',)
                )
            with self.assertRaisesRegex(RuntimeError, "another generation"):
                self.store.save_dirty()
            with self.assertRaisesRegex(RuntimeError, "another generation"):
                self.store.save_dirty()
        self.install()
        self.assertEqual(self.store.get("probe")["priority"], 5)
