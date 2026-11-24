"""Infrastructure plugin facades in one YGL module generation."""

from dataclasses import dataclass
import importlib
from pathlib import Path
import sys
import tomllib
from typing import Literal

from port_errors import Unsupported


@dataclass(frozen=True)
class PluginKindMeta:
    kind: Literal["meta"]
    provides: str
    plugin_name: str


def read_meta_plugin_info(plugin):
    plugin = Path(plugin)
    if not plugin.is_dir():
        return None
    marked = (plugin / "__meta_plugin__.py").is_file()
    provides = plugin.stem
    file = plugin / "pyproject.toml"
    if file.is_file():
        try:
            with file.open("rb") as stream:
                config = tomllib.load(stream)
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            from gsuid_core.logger import logger

            logger.error("Invalid plugin metadata")
        else:
            tool = config.get("tool")
            options = tool.get("gsuid") if isinstance(tool, dict) else None
            if isinstance(options, dict):
                marked |= options.get("kind") == "meta"
                value = options.get("provides")
                if isinstance(value, str) and value.strip():
                    provides = value.strip()
    if marked:
        return PluginKindMeta("meta", provides, plugin.stem)
    return None


class MetaPluginNotFoundError(RuntimeError):
    def __init__(self, provides):
        self.provides = provides
        super().__init__(f"基础设施插件 {provides!r} 未加载。")


class MetaRegistry:
    def __init__(self, owner):
        self.owner = owner
        self.sources = {}
        self.providers = {}
        self.aliases = {}

    def check_owner(self, name):
        if name != self.owner:
            raise Unsupported(
                "cannot manage infrastructure providers in another profile"
            )

    def register_meta_plugin(self, provides, module, plugin_name):
        self.check_owner(plugin_name)
        self.sources[provides] = module
        self.providers[plugin_name] = provides
        self.aliases[plugin_name + ".api"] = module
        parent = sys.modules.get(module.__name__.rpartition(".")[0])
        if parent is not None:
            self.aliases[plugin_name] = parent
        sys.modules.update(self.aliases)

    def unregister_meta_plugin(self, plugin_name):
        self.check_owner(plugin_name)
        for name, module in self.aliases.items():
            if sys.modules.get(name) is module:
                del sys.modules[name]
        self.aliases.clear()
        self.sources.pop(self.providers.pop(plugin_name, None), None)

    def require(self, provides):
        if provides not in self.sources:
            raise MetaPluginNotFoundError(provides)
        return self.sources[provides]

    def import_api(self, package):
        if package != self.owner:
            return None
        try:
            return importlib.import_module(package + ".api")
        except ImportError:
            return None

    def provides_of(self, plugin_name):
        return self.providers.get(plugin_name)

    def list_meta_plugins(self):
        return self.providers.copy()

    def bind_meta_plugin_facade(self, provides, plugin_name, module_names):
        self.check_owner(plugin_name)
        candidates = [name for name in module_names if name.endswith(".api")]
        candidates.extend(
            (f"plugins.{plugin_name}.{plugin_name}.api", f"plugins.{plugin_name}.api")
        )
        for name in candidates:
            if name in sys.modules:
                self.register_meta_plugin(provides, sys.modules[name], plugin_name)
                return True
        return False


def install_meta(module, server, owner):
    registry = MetaRegistry(owner)
    for name in (
        "register_meta_plugin",
        "unregister_meta_plugin",
        "require",
        "import_api",
        "provides_of",
        "list_meta_plugins",
        "bind_meta_plugin_facade",
    ):
        setattr(module, name, getattr(registry, name))
    module.MetaPluginNotFoundError = MetaPluginNotFoundError
    server.PluginKindMeta = PluginKindMeta
    server.read_meta_plugin_info = read_meta_plugin_info
    return registry
