"""Authorized routing settings affect future registrations in the active profile."""

from contextvars import copy_context
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
import sayu_compat as loader
from port_errors import Unsupported
from service_settings import management_scope


class RoutingTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        loader.install("sample", "sp", self.root, runtime_mode="package")
        self.plugin = loader.Plugins(
            "sample", prefix=["old"], force_prefix=["forced"], alias=["declared"]
        )
        self.service = loader.SV("query")

    def test_ordinary_and_expired_grants_cannot_edit_routing(self):
        from gsuid_core.config import plugin_config_store

        with management_scope(writable=True):
            arg = ["new"]
            self.plugin.set(prefix=arg)
            old_grant = copy_context()
            prefixes = self.plugin.prefix
            aliases = plugin_config_store.get("sample")["alias"]
        arg.append("retained-argument")
        for action in [
            lambda: self.plugin.set(prefix=["ordinary"]),
            lambda: setattr(self.plugin, "prefix", ["ordinary"]),
            lambda: prefixes.append("ordinary"),
            lambda: old_grant.run(aliases.clear),
            lambda: aliases.__setitem__(slice(None), ["ordinary"]),
        ]:
            with self.assertRaises(Unsupported):
                action()
        self.assertEqual(self.plugin.prefix, ["new"])
        self.assertEqual(self.plugin.alias, ["declared"])
        loader.install("another", "an", self.root / "another", runtime_mode="package")
        with management_scope(writable=True), self.assertRaises(Unsupported):
            prefixes.append("another")

    def test_bulk_changes_validate_before_changing_routes_or_acl(self):
        from gsuid_core.config import plugin_config_store

        with management_scope(writable=True):
            for changes in [
                {"prefix": ["partial"], "pm": 0},
                {"prefix": ["partial"], "area": "ALL"},
                {"prefix": ["partial"], "allow_empty_prefix": "no"},
                {"prefix": ["valid", 2]},
            ]:
                with self.assertRaises((ValueError, Unsupported)):
                    self.plugin.set(**changes)
                self.assertEqual(self.plugin.prefix, ["old"])
                self.assertEqual(self.plugin.pm, 6)
            with self.assertRaises(Unsupported):
                self.service.set(prefix=["sv"], priority=1)
            self.assertEqual(self.service.priority, 5)
            with self.assertRaises(Unsupported):
                plugin_config_store.set("other", {"prefix": ["foreign"]})
            with self.assertRaises(ValueError):
                self.plugin.alias.extend(["valid", False])
            self.assertEqual(self.plugin.alias, ["declared"])

    def test_retained_lists_are_live_until_replaced_and_reload_uses_declaration(self):
        from gsuid_core.config import plugin_config_store

        with management_scope(writable=True):
            values = plugin_config_store.get("sample")
            old = values["prefix"]
            self.assertIs(old, self.plugin.prefix)
            old.extend(["appended"])
            self.assertEqual(self.plugin.prefixes, ("old", "appended", "forced"))
            self.plugin.set(
                prefix=["new"], alias=["override"], force_prefix=["override-force"]
            )
            old[0] = "detached"
            self.assertEqual(values["prefix"], ["new"])
            plugin_config_store.save_all()
        loader.install("sample", "sp", self.root, runtime_mode="package")
        explicit = loader.Plugins(
            "sample", prefix=["default"], force_prefix=["latest"], alias=["updated"]
        )
        self.assertEqual(explicit.prefix, ["new"])
        self.assertEqual(explicit.force_prefix, ["latest"])
        self.assertEqual(explicit.alias, ["updated"])

    def test_implicit_registration_restores_profile_prefix_metadata(self):
        with management_scope(writable=True):
            self.plugin.set(False, force_prefix=["kept"], alias=["retained"])
        loader.install("sample", "sp", self.root, runtime_mode="package")
        inferred = loader.SV("implicit").plugins
        self.assertEqual(inferred.force_prefix, ["kept"])
        self.assertEqual(inferred.alias, ["retained"])
