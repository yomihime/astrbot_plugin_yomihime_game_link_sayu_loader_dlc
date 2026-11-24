"""Compare framework and plugin catalog lifecycles without booting either server."""

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / ".sayu-local/reference"
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT / "scripts"))
from locale_assets import I18N_DIGEST, MEMBER, verified_locales  # noqa: E402
from plugin_i18n import Catalog  # noqa: E402


def exercise(api, work, config):
    def create(file, content):
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")

    plugin = work / "example"
    locale = plugin / "locales"
    create(
        locale / "en.json",
        {
            "lang.name": "conflict",
            "x.title": "{who}",
            "📦 example": "record",
            "log.core.sample": "event",
            "nonstr": 3,
        },
    )
    create(locale / "zh-cn" / "a.json", {"x.title": "first"})
    create(locale / "zh-cn" / "b.json", {"x.title": "问候 {who}", "only.zh": "中文"})
    create(locale / "custom-lang" / "x.json", {"lang.name": "Custom"})
    other = work / "other"
    create(other / "locales" / "en.json", {"x.title": "collision", "other": "second"})
    result = []
    result.append(
        (api.available_language_codes(), api.lang_display_name("ja"), api.get_lang())
    )
    result.append(api.discover_and_register_plugin_locales(plugin))
    result.append(api.discover_and_register_plugin_locales(other))
    result.append(
        (
            api.supported_langs(),
            api.plugin_locale_key_count("example"),
            api.t("x.title", who="A"),
        )
    )
    config["LANGUAGE"] = "en"
    result.append(
        (
            api.t("x.title", lang="invalid", who="B"),
            api.t("only.zh"),
            api.t("example"),
            api.t("📦 example"),
            api.t("log.core.sample"),
        )
    )
    try:
        api.t("x.title", unexpected="x")
    except KeyError as exc:
        result.append(("KeyError", exc.args))
    create(locale / "en.json", {"x.new": "replacement", "lang.name": "override"})
    result.append(
        api.register_plugin_locales(
            "example", locale, reload=True, override_framework=True
        )
    )
    result.append((api.lang_display_name("en"), api.t("x.new")))
    api.unregister_plugin_locales("example")
    result.append((api.t("x.new"), api.t("lang.name"), api.t("other")))
    api.load_catalogs()
    config["LANGUAGE"] = "invalid"
    result.append(
        (api.lang_display_name("en"), api.get_lang(), api.t("other", lang="en"))
    )
    return result


class LocaleTest(unittest.TestCase):
    def test_lifecycle_without_framework_checkout(self):
        framework = {
            "catalogs": {"en": {"shared": "base"}, "zh-cn": {}},
            "emoji": {"misc": "!"},
            "leading": r"^📦\s*",
        }
        with tempfile.TemporaryDirectory() as tmp:
            plugin = Path(tmp) / "Game"
            locales = plugin / "locales"
            locales.mkdir(parents=True)
            (locales / "en.json").write_text('{"shared":"override","new":"user {who}"}')
            api = Catalog(tmp, data=framework, read_language=lambda: "en")
            self.assertEqual(api.discover_and_register_plugin_locales(plugin), 1)
            self.assertEqual(api.t("shared"), "base")
            self.assertEqual(api.t("new", who="A"), "user A")
            api.unregister_plugin_locales("Game")
            self.assertEqual(api.t("new"), "new")
            self.assertEqual(api.t("log.unknown.message"), "! log.unknown.message")
            with self.assertRaises(ValueError):
                api.register_plugin_locales("outside", Path(tmp).parent)

    @unittest.skipUnless(
        (REFERENCE / "gsuid_core/i18n.py").is_file(),
        "needs pinned native comparison source",
    )
    def test_native_comparison(self):
        path = REFERENCE / "gsuid_core/i18n.py"
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), I18N_DIGEST)
        spec = importlib.util.spec_from_file_location("_native_i18n_contract", path)
        native = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(native)
        assets = json.loads(verified_locales(REFERENCE)[MEMBER])
        with tempfile.TemporaryDirectory() as tmp:
            config = {"LANGUAGE": "zh-cn"}
            core = ModuleType("gsuid_core.config")
            core.core_config = SimpleNamespace(get_config=config.get)
            with patch.dict(sys.modules, {"gsuid_core.config": core}):
                expected = exercise(native, Path(tmp) / "native", config)
            config["LANGUAGE"] = "zh-cn"
            api = Catalog(tmp, assets, lambda: config["LANGUAGE"])
            actual = exercise(api, Path(tmp) / "port", config)
            self.assertEqual(actual, expected)
