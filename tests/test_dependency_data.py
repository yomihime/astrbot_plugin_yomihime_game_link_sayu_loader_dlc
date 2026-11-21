import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
import dependency_data as data


class DependencyDataTest(unittest.TestCase):
    def tearDown(self):
        data._table.cache_clear()
        data._verify_tables.cache_clear()

    def test_only_data_is_visible_and_deeply_readonly(self):
        provider = SimpleNamespace(
            table={"nested": [["original"]]}, reload=lambda: None
        )
        with (
            patch.object(data, "_verify_tables"),
            patch.object(data, "import_module", return_value=provider),
        ):
            model = data.dependency_import("starrail_damage_cal.excel", "model")
            self.assertEqual(model.table["nested"], (("original",),))
            with self.assertRaises(TypeError):
                model.table["nested"] = ()
            with self.assertRaises(TypeError):
                model.table["nested"][0][0] = "edit"
            with self.assertRaises(AttributeError):
                _ = model.reload
            with self.assertRaises(KeyError):
                data.dependency_import("starrail_damage_cal.excel.model", "reload")
            with self.assertRaises(KeyError):
                data.dependency_import("starrail_damage_cal.update", "update")
            self.assertEqual(provider.table, {"nested": [["original"]]})

    def test_missing_or_changed_tables_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            dist = SimpleNamespace(version="4.6.0", locate_file=lambda _: Path(folder))
            with patch.object(data.metadata, "distribution", return_value=dist):
                with self.assertRaisesRegex(ValueError, "data tables"):
                    data.dependency_import("starrail_damage_cal.map", "SR_MAP_PATH")

    @unittest.skipUnless(
        importlib.util.find_spec("starrail_damage_cal"),
        "needs the StarRail private environment",
    )
    def test_installed_wheel_tables_match_upstream(self):
        from starrail_damage_cal.excel import model
        from starrail_damage_cal.map import SR_MAP_PATH

        tables = data.dependency_import("starrail_damage_cal.excel", "model")
        self.assertEqual(
            list(tables.CharAlias["characters"]), list(model.CharAlias["characters"])
        )
        self.assertEqual(
            tables.AvatarPromotionConfig[0].AvatarID,
            model.AvatarPromotionConfig[0].AvatarID,
        )
        self.assertEqual(
            data.dependency_import(
                "starrail_damage_cal.map.SR_MAP_PATH", "avatarId2Name"
            ),
            SR_MAP_PATH.avatarId2Name,
        )
        with self.assertRaises(AttributeError):
            _ = tables.reload_excel_data
