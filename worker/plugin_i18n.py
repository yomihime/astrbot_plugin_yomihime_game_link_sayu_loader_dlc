"""One profile's translation catalogs, initialized from verified framework data."""

import json
import pkgutil
import re
from enum import Enum
from functools import cache
from pathlib import Path


class Lang(str, Enum):
    ZH_CN = "zh-cn"
    EN = "en"
    JA = "ja"


DEFAULT_LANG = Lang.ZH_CN.value


@cache
def _framework():
    try:
        raw = pkgutil.get_data(__name__, "plugin_locales.json")
    except FileNotFoundError:
        # Source-tree diagnostics run without the zipapp. Use the same verified,
        # offline build inputs as production without fetching anything at runtime.
        root = Path(__file__).resolve().parents[1]
        reference = root / ".sayu-local/reference"
        builder = root / "scripts/locale_assets.py"
        if not reference.is_dir() or not builder.is_file():
            raise
        from importlib.util import module_from_spec, spec_from_file_location

        spec = spec_from_file_location("_sayu_locale_assets", builder)
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        raw = module.verified_locales(reference)[module.MEMBER]
    if raw is None:
        raise RuntimeError("missing worker locale data")
    return json.loads(raw)


def _configured_language():
    try:
        from gsuid_core.config import core_config
    except ImportError:
        return DEFAULT_LANG
    return core_config.get_config("LANGUAGE")


class Catalog:
    def __init__(self, source_root, data=None, read_language=_configured_language):
        self.root = Path(source_root).resolve()
        self.framework = data
        self.read_language = read_language
        self.catalogs = {}
        self.roots = {}
        self.keys = {}
        self.aliases = {}

    def _ready(self):
        if not self.catalogs:
            self.load_catalogs()

    def load_catalogs(self):
        if self.framework is None:
            self.framework = _framework()
        self.leading = re.compile(self.framework["leading"])
        self.catalogs = {
            lang: dict(table) for lang, table in self.framework["catalogs"].items()
        }
        roots = dict(self.roots)
        self.roots.clear()
        self.keys.clear()
        self.aliases.clear()
        for lang in self.catalogs:
            self._index(lang)
        for name, root in roots.items():
            self.register_plugin_locales(name, root)

    def strip_leading_emoji(self, text):
        self._ready()
        for _ in range(2):
            text = self.leading.sub("", text, count=1)
        return text

    def starts_with_emoji(self, text):
        return bool(text) and text != self.strip_leading_emoji(text)

    def _index(self, lang):
        aliases = {}
        for key in self.catalogs[lang]:
            bare = self.strip_leading_emoji(key)
            if bare and bare != key:
                aliases.setdefault(bare, key)
        self.aliases[lang] = aliases

    def supported_langs(self):
        self._ready()
        return list(self.catalogs)

    def is_supported(self, lang):
        self._ready()
        return bool(lang) and lang in self.catalogs

    def get_lang(self):
        lang = self.read_language()
        return lang if self.is_supported(lang) else DEFAULT_LANG

    def lang_display_name(self, code):
        name = self.t("lang.name", lang=code)
        return code if name == "lang.name" else name

    def available_language_codes(self):
        return [lang.value for lang in Lang if self.is_supported(lang.value)]

    @staticmethod
    def log_module_of_key(key):
        parts = key.split(".", 2)
        return parts[1] if len(parts) == 3 and parts[0] == "log" and parts[1] else None

    def emoji_for_log_key(self, key):
        self._ready()
        module = self.log_module_of_key(key)
        if module:
            return self.framework["emoji"].get(
                module.lower(), self.framework["emoji"]["misc"]
            )
        return None

    def ensure_log_emoji(self, key, text):
        if not text or not key.startswith("log.") or self.starts_with_emoji(text):
            return text
        prefix = self.emoji_for_log_key(key)
        return f"{prefix} {text}" if prefix else text

    def t(self, key, /, lang=None, **params):
        lang = lang if self.is_supported(lang) else self.get_lang()
        cat = self.catalogs.get(lang, {})
        bare = self.strip_leading_emoji(key)
        alias = self.aliases.get(lang, {}).get(bare)
        template = cat.get(key, cat.get(bare, cat.get(alias, key)))
        return self.ensure_log_emoji(
            key, template.format(**params) if params else template
        )

    def _path(self, value):
        path = Path(value).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("locale path outside this profile")
        return path

    def register_plugin_locales(
        self, plugin_name, locales_root, *, reload=False, override_framework=False
    ):
        self._ready()
        root = self._path(locales_root)
        if reload:
            self.unregister_plugin_locales(plugin_name)
        files = []
        if root.is_dir():
            for path in sorted(root.iterdir()):
                if path.is_file() and path.suffix == ".json":
                    files.append((path.stem, self._path(path)))
                elif path.is_dir() and not path.name.startswith((".", "_")):
                    files.extend(
                        (path.name, self._path(file))
                        for file in sorted(self._path(path).glob("*.json"))
                    )
        if not files:
            return 0
        owned = self.keys.setdefault(plugin_name, set())
        self.roots[plugin_name] = root
        count = 0
        for lang, path in files:
            try:
                values = json.loads(path.read_text(encoding="utf8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(values, dict):
                continue
            table = self.catalogs.setdefault(lang, {})
            for key, text in values.items():
                if not isinstance(text, str) or (
                    key in table and key not in owned and not override_framework
                ):
                    continue
                table[key] = text
                owned.add(key)
                count += 1
            self._index(lang)
        return count

    def unregister_plugin_locales(self, plugin_name):
        self.roots.pop(plugin_name, None)
        owned = self.keys.pop(plugin_name, set())
        if owned:
            for lang, table in self.catalogs.items():
                for key in owned:
                    table.pop(key, None)
                self._index(lang)

    def discover_and_register_plugin_locales(self, plugin_dir, plugin_name=None):
        path = self._path(plugin_dir)
        locales = self._path(path / "locales")
        return (
            self.register_plugin_locales(plugin_name or path.name, locales, reload=True)
            if locales.is_dir()
            else 0
        )

    def plugin_locale_key_count(self, plugin_name):
        return len(self.keys.get(plugin_name, ()))


def install_i18n(module, *, source_root=None, read_language=_configured_language):
    catalog = Catalog(source_root or Path.cwd(), read_language=read_language)
    module.Lang = Lang
    module.DEFAULT_LANG = DEFAULT_LANG
    for name in vars(Catalog):
        if not name.startswith("_"):
            setattr(module, name, getattr(catalog, name))
    return catalog
