"""Generation-local registry and bounded public ports for the pinned help renderers."""

from functools import cache
from importlib import import_module
from inspect import signature
from io import BytesIO
from pathlib import Path
import pkgutil
import sys
from tempfile import TemporaryDirectory
from threading import RLock
from typing import List, TypedDict
from zipfile import ZipFile

from PIL import Image, ImageFont

from port_errors import Unsupported


class PluginSV(TypedDict):
    name: str
    desc: str
    eg: str
    highlight: int
    need_ck: bool
    need_sk: bool
    need_admin: bool


class PluginHelp(TypedDict):
    desc: str
    data: List[PluginSV]


class _Assets:
    def __init__(self, data, source):
        self.data = Path(data).resolve()
        self.source = Path(source).resolve() if source else self.data
        self.directory = None
        self.lock = RLock()

    def root(self):
        with self.lock:
            if self.directory is None:
                raw = pkgutil.get_data(__name__, "plugin_help_assets.zip")
                if raw is None:
                    raise Unsupported("verified help assets are unavailable")
                self.data.mkdir(parents=True, exist_ok=True)
                temp = TemporaryDirectory(prefix="help-assets-", dir=self.data)
                try:
                    with ZipFile(BytesIO(raw)) as archive:
                        # This archive is created from immutable, checksum-verified files.
                        archive.extractall(temp.name)
                except BaseException:
                    temp.cleanup()
                    raise
                self.directory = temp
            return Path(self.directory.name)


_assets = None


def asset_root():
    return _assets.root()


def core_font(size, weight=630.0):
    font = ImageFont.truetype(str(asset_root() / "MiSansVF.ttf"), size=size)
    if callable(adjust := getattr(font, "set_variation_by_axes", None)):
        try:
            adjust([min(700.0, max(150.0, float(weight)))])
        except (OSError, ValueError, TypeError):
            pass
    return font


def _filename(name):
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 160
        or name in {".", ".."}
        or name[-1] in " ."
        or any(ord(ch) < 32 or ch in '<>:"/\\|?*[]' for ch in name)
        or Path(name).is_reserved()
    ):
        raise Unsupported("invalid help cache name")


def _safe_image(path):
    root = _assets
    file = Path(path).resolve()
    if not any(file.is_relative_to(p) for p in (root.source, root.data)):
        raise Unsupported("help image outside profile")
    if file.is_file() and file.stat().st_nlink != 1:
        raise Unsupported("linked help image outside profile")
    return file


def open_help_image(path):
    from sayu_compat import MAX_INPUT_IMAGE_BYTES, MAX_INPUT_PIXELS

    source = _safe_image(path)
    if source.stat().st_size > MAX_INPUT_IMAGE_BYTES:
        raise Unsupported("help image exceeds the worker byte limit")
    image = Image.open(source)
    if image.width * image.height > MAX_INPUT_PIXELS:
        image.close()
        raise Unsupported("help image exceeds the worker pixel limit")
    return image


def _cache_file(name):
    from gsuid_core.data_store import get_res_path

    _filename(name)
    path = get_res_path("help") / name
    if path.resolve() != path or (path.exists() and path.stat().st_nlink != 1):
        raise Unsupported("help cache outside profile")
    return path


@cache
def _renderer(modern):
    suffix = "draw_new_plugin_help" if modern else "draw_plugin_help"
    return import_module("plugin_help_native." + suffix)


def _port(modern):
    async def render(*args, **kwargs):
        owner = _renderer(modern)
        target = getattr(owner, "get_new_help" if modern else "get_help")
        call = signature(target).bind(*args, **kwargs)
        call.apply_defaults()
        params = call.arguments
        name = params["plugin_name" if modern else "name"]
        _filename(name)
        pm = params.get("pm", 6)
        if type(pm) is not int:
            raise Unsupported("invalid help permission")
        _cache_file(f"{name}_{pm}.jpg" if modern else f"{name}.jpg")
        column = params["column"]
        if type(column) is not int or not 1 <= column <= 16:
            raise Unsupported("invalid help column count")
        if modern and not 0 < params["plugin_icon_size"] <= 2048:
            raise Unsupported("invalid help icon dimensions")
        icon_path = params["icon_path" if modern else "ICON_PATH"]
        if icon_path is not None:
            _safe_image(icon_path)
        help_data = params["plugin_help" if modern else "help_data"]
        for group in help_data.values():
            for entry in group["data"]:
                if "icon" in entry and not isinstance(entry["icon"], Image.Image):
                    _safe_image(entry["icon"])
                if not modern:
                    _filename(entry["name"])
        if not modern:
            # The classic renderer allocates one transparent canvas per group
            # before calling the common bounded crop helper.
            from sayu_compat import MAX_INPUT_PIXELS

            width = 50 + 260 * column
            height = 630 + (100 if params["extra_message"] else 0)
            for group in help_data.values():
                height += 100 + ((len(group["data"]) + column - 1) // column) * 103
            if width * height > MAX_INPUT_PIXELS:
                raise Unsupported("help image exceeds the worker limit")
        return await target(*args, **kwargs)

    return render


def install_help(package, data_root, *, source_root=None):
    global _assets
    _assets = _Assets(data_root, source_root)
    _renderer.cache_clear()
    for suffix in ("draw_new_plugin_help", "draw_plugin_help", "image_helpers"):
        sys.modules.pop("plugin_help_native." + suffix, None)
    package("gsuid_core.help")
    model = package("gsuid_core.help.model")
    model.PluginSV, model.PluginHelp = PluginSV, PluginHelp
    utils = package("gsuid_core.help.utils")
    category = {"desc": "这里可以看到注册过的插件帮助。", "data": []}
    utils.plugins_help = {"插件帮助一览": category}

    def register_help(name, help, icon=None):
        if icon is None:
            with Image.open(asset_root() / "ICON.png") as default:
                icon = default.convert("RGBA")
        elif icon.mode != "RGBA":
            icon = icon.convert("RGBA")
        item = {
            "name": name,
            "desc": f"{name}插件帮助功能",
            "eg": f"发送 {help} 获得帮助",
            "icon": icon,
            "need_ck": False,
            "need_sk": False,
            "need_admin": False,
        }
        if item not in category["data"]:
            category["data"].append(item)

    def clean_plugin_help(plugin_name):
        _filename(plugin_name)
        from gsuid_core.data_store import get_res_path

        for name in {plugin_name, "GsCore"}:
            for store in (modern.cache, classic.cache):
                store.pop(name, None)
            for path in get_res_path("help").glob(f"{name}_*.jpg"):
                _cache_file(path.name).unlink(missing_ok=True)
            _cache_file(name + ".jpg").unlink(missing_ok=True)
        category["data"] = [
            item for item in category["data"] if item["name"] != plugin_name
        ]

    utils.register_help = register_help
    utils.clean_plugin_help = clean_plugin_help
    modern = package("gsuid_core.help.draw_new_plugin_help")
    classic = package("gsuid_core.help.draw_plugin_help")
    for port, style, member in (
        (modern, True, "get_new_help"),
        (classic, False, "get_help"),
    ):
        setattr(port, member, _port(style))
        port.__getattr__ = _public_cache(style)
    fonts = package("gsuid_core.utils.fonts")
    font_module = package("gsuid_core.utils.fonts.fonts")
    fonts.core_font = font_module.core_font = core_font

    def font_attr(key):
        if key == "FONT_ORIGIN_PATH":
            return asset_root() / "MiSansVF.ttf"
        raise AttributeError(key)

    font_module.__getattr__ = font_attr
    tools = sys.modules["gsuid_core.utils.image.image_tools"]

    def tint_image(*args, **kwargs):
        return import_module("plugin_help_native.image_helpers").tint_image(
            *args, **kwargs
        )

    def draw_color_badge(*args, **kwargs):
        return import_module("plugin_help_native.image_helpers").draw_color_badge(
            *args, **kwargs
        )

    tools.tint_image, tools.draw_color_badge = tint_image, draw_color_badge


def _public_cache(modern):
    def get(name):
        if name == "cache":
            return _renderer(modern).cache
        raise AttributeError(name)

    return get
