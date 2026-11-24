"""Configuration set management, durability and profile isolation."""

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))

from plugin_config_models import GsStrConfig
from plugin_settings import configuration_manager_type, configuration_type
from port_errors import Unsupported
from service_settings import management_scope


class ConfigManagerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "profile"
        self.root.mkdir()
        self.kind = configuration_type(self.root, "fixture")
        self.manager_kind = configuration_manager_type(self.root, self.kind)
        self.fields = {"field": GsStrConfig("配置", "", "original")}
        self.manager = self.manager_kind(self.root / "sets", self.fields)
        with management_scope(writable=True):
            self.manager.create_default("first")

    def test_ordinary_context_cannot_write_files_or_evict_named_instances(self):
        m = self.manager
        source = m.get_config("first")
        old = source.CONFIG_PATH.read_bytes()
        source["field"].data = "ordinary"
        for action in [
            lambda: source.set_config("field", "ordinary"),
            lambda: m.copy("first", "second"),
            lambda: m.create_default("second"),
            lambda: m.delete("first"),
            lambda: m.drop_cache("first"),
            m.clear_cache,
        ]:
            with self.assertRaises(Unsupported):
                action()
        self.assertIs(source, m.get_config("first"))
        self.assertEqual(source["field"].data, "original")
        self.assertEqual(m.get_config("second")["field"].data, "original")
        self.assertFalse(m.exists("second"))
        self.assertEqual(source.CONFIG_PATH.read_bytes(), old)

        with management_scope(writable=True):
            captured = copy_context()
        with self.assertRaises(Unsupported):
            captured.run(m.delete, "first")

    def test_same_name_and_path_validation_are_bound_to_one_profile(self):
        other = self.root.parent / "other"
        other.mkdir()
        other_type = configuration_type(other, "fixture")
        other_manager = configuration_manager_type(other, other_type)(
            other, self.fields
        )
        with management_scope(writable=True):
            one = self.manager.get_config("first")
            two = other_manager.get_config("first")
            two.set_config("field", "second profile")
            self.assertIsNot(one, two)
            self.assertEqual(one["field"].data, "original")
            with self.assertRaises(ValueError):
                self.manager_kind(other, self.fields)
            for name in ["../other/first", "..", "sub/path", "x:y", "bad\x00file"]:
                with self.subTest(name=name), self.assertRaises(ValueError):
                    self.manager.copy("first", name)
            with self.assertRaises(ValueError):
                self.kind("move", [two.CONFIG_PATH, one.CONFIG_PATH], self.fields)
            self.assertEqual(two["field"].data, "second profile")

    def test_external_aliases_and_directory_targets_are_rejected(self):
        external = self.root.parent / "outside.json"
        external.write_bytes(b"external fixture")
        alias = self.root / "sets/alias.json"
        try:
            os.link(external, alias)
        except PermissionError:
            self.skipTest("filesystem denied creation of the fixture hard link")
        with management_scope(writable=True):
            with self.assertRaises(ValueError):
                self.manager.copy("alias", "saved")
            with self.assertRaises(ValueError):
                self.manager.delete("alias")
            (self.root / "sets/folder.json").mkdir()
            with self.assertRaises(ValueError):
                self.manager.delete("folder")
        self.assertEqual(external.read_bytes(), b"external fixture")
        self.assertFalse(self.manager.exists("saved"))

    def test_competing_copies_publish_complete_source_once(self):
        source = self.manager.get_config("first").CONFIG_PATH

        def copy_once(_):
            with management_scope(writable=True):
                return self.manager.copy("first", "dest")

        with ThreadPoolExecutor(max_workers=8) as executor:
            outputs = list(executor.map(copy_once, range(20)))
        self.assertEqual(outputs.count(None), 1)
        self.assertEqual(outputs.count("配置文件 'dest' 已存在"), 19)
        self.assertEqual(
            source.with_name("dest.json").read_bytes(), source.read_bytes()
        )
        self.assertEqual(
            sorted(path.name for path in source.parent.iterdir()),
            ["dest.json", "first.json"],
        )

    def test_partial_copy_and_migration_publication_failures_preserve_original(self):
        source = self.manager.get_config("first").CONFIG_PATH
        payload = source.read_bytes()
        with management_scope(writable=True):
            with patch("shutil.copyfileobj", side_effect=OSError("fixture failure")):
                with self.assertRaises(OSError):
                    self.manager.copy("first", "dest")
            with patch(
                "plugin_settings._publish_new", side_effect=OSError("fixture failure")
            ):
                with self.assertRaises(OSError):
                    self.kind(
                        "move", [source, source.with_name("dest.json")], self.fields
                    )
        self.assertEqual([p.name for p in source.parent.iterdir()], ["first.json"])
        self.assertEqual(source.read_bytes(), payload)


if __name__ == "__main__":
    unittest.main()
