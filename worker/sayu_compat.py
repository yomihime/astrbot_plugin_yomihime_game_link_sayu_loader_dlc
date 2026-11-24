"""Worker-local host ports for upstream chat services."""

import base64
import binascii
import asyncio
import sys
import uuid
from collections import Counter, deque
from contextlib import contextmanager
from copy import deepcopy
from io import BytesIO
from os import PathLike
from pathlib import Path
from types import ModuleType

from plugin_settings import install_settings
from plugin_image_tools import install_image_tools
from plugin_help import install_help
from plugin_image_fetch import install_image_fetch
from plugin_data_store import install_data_store
from plugin_downloads import install_downloads
from plugin_i18n import install_i18n
from plugin_core_config import install_core_config
from plugin_logger import install_logger
from plugin_minigg import install_minigg
from plugin_public_api import install_public_api
from plugin_public_http import PublishedImages
from plugin_messages import (
    Button,
    ButtonType,
    ButtonList,
    Event,
    Message,
    MessageReceive,
    MessageSend,
)
from service_settings import select_service_profile
from plugin_app_life import install_app_life
from plugin_ai import install_ai
from plugin_qrlogin import install_qrlogin
from plugin_status import install_status
from plugin_pool import install_pool
from plugin_user_language import bot_get_lang, bot_t
import plugin_trigger
import plugin_text_image
from port_errors import Unsupported
from target_transport import UnsupportedMessage, file_payload


class NoMatch(LookupError):
    """No available service accepts this command and conversation."""


_routes = []
_plugin_settings = {}
_services = {}
_service_store = None
_prefix = ""
_plugin = ""
_data_root = None
_source = None
MAX_INPUT_IMAGE_BYTES = 16 * 1024 * 1024
MAX_INPUT_PIXELS = 12_000_000
_MAX_TEXT = 4096
_MAX_PARTS = 32


def package(name, directory=None):
    module = ModuleType(name)
    module.__path__ = [] if directory is None else [str(directory)]
    module.__package__ = name
    sys.modules[name] = module
    parent, _, child = name.rpartition(".")
    if parent and parent in sys.modules:
        setattr(sys.modules[parent], child, module)
    return module


@contextmanager
def registration_scope():
    start = len(_routes)
    try:
        yield
    except BaseException:
        del _routes[start:]
        raise


def registration_counts():
    """Counts of distinct services and handlers, including multiple aliases per handler."""
    return {
        "services": len({id(route.sv) for route in _routes}),
        "handlers": len({route.func for route in _routes}),
        "triggers": len(_routes),
    }


def registered_triggers():
    """List registrations without invoking game code or inspecting any user data."""
    return [
        {
            "service": route.sv.name,
            "handler": route.func.__module__ + "." + route.func.__qualname__,
            "mode": route.type,
            "pattern": route.keyword,
            "prefix": bool(route.prefix),
            "area": route.sv.area,
        }
        for route in _routes
    ]


def check_redeclaration(instance, options):
    for key, declared in instance._restrictions.items():
        if key in options:
            value = tuple(options[key]) if key.endswith("_list") else options[key]
            if value != declared:
                raise Unsupported("conflicting permission declarations for one name")


class SVList:
    def __init__(self):
        self.lst = {}
        self.plugins = {}
        self.detail_lst = {}

    @property
    def get_lst(self):
        return self.lst


class SV:
    def __new__(cls, name="", *args, **kwargs):
        if name is None:
            raise ValueError("SV.name is None!")
        if _service_store is not None and name in _services:
            positional = dict(
                zip(
                    (
                        "pm",
                        "priority",
                        "enabled",
                        "area",
                        "black_list",
                        "white_list",
                        "aside",
                    ),
                    args,
                )
            )
            check_redeclaration(_services[name], {**positional, **kwargs})
            return _services[name]
        return super().__new__(cls)

    def __init__(
        self,
        name="",
        pm=6,
        priority=5,
        enabled=True,
        area="ALL",
        black_list=(),
        white_list=(),
        aside=None,
        **options,
    ):
        if options:
            raise Unsupported("unsupported service options")
        if getattr(self, "_initialized", False):
            if aside is not None:
                self.aside = aside
            return
        self.name = name
        self.pm = pm
        self.priority = priority
        self.area = area
        self.enabled = enabled
        self.aside = aside
        self.TL = {}
        self.self_plugin_name = _plugin
        self.white_list, self.black_list = list(white_list), list(black_list)
        self._restrictions = {
            "pm": pm,
            "area": area,
            "black_list": tuple(black_list),
            "white_list": tuple(white_list),
        }
        self._acl_blocked = bool(black_list or white_list)
        if _service_store is not None:
            _service_store.restore("service", self)
            _services[name] = self
            self.plugins = _plugin_settings.get(_plugin) or Plugins(
                _plugin, force_prefix=[_prefix] if _prefix else []
            )
            sys.modules["gsuid_core.sv"].SL.detail_lst.setdefault(
                self.plugins, []
            ).append(self)
        self._initialized = True

    def set(self, is_lazy=True, **changes):
        if _service_store is None or _services.get(self.name) is not self:
            raise Unsupported("service management requires the active package")
        _service_store.update("service", self, changes, is_lazy)

    def enable(self):
        self.set(enabled=True)

    def disable(self):
        self.set(enabled=False)

    def register(
        self,
        mode,
        commands,
        block=False,
        to_me=False,
        prefix=True,
        to_ai="",
        covers=None,
        aliases=None,
        aside=None,
        **options,
    ):
        def decorate(handler):
            if options:
                raise Unsupported("unsupported trigger options")
            if _service_store is None and (not self.enabled or self._acl_blocked):
                raise Unsupported("restricted service")
            words = (commands,) if isinstance(commands, str) else commands
            for word in dict.fromkeys(words):
                if not isinstance(word, str) or not word:
                    raise ValueError("invalid trigger keyword")
                settings = _plugin_settings.get(_plugin)
                prefixes = settings.prefixes if settings else (_prefix,)
                prefixed = (
                    prefix and prefixes and mode not in {"message", "meta", "file"}
                )
                for literal_prefix in prefixes if prefixed else ("",):
                    route = plugin_trigger.Trigger(
                        mode, word, handler, literal_prefix, block, to_me, aside, self
                    )
                    route.ai_description = to_ai
                    route.covers = covers
                    route.aliases = aliases
                    plugin_trigger.bump_registry_version()
                    table = self.TL.setdefault(mode, {})
                    key = literal_prefix + word
                    if key in table:
                        _routes.remove(table[key])
                    table[key] = route
                    _routes.append(route)
            return handler

        return decorate

    def on_command(self, commands, *args, **options):
        return self.register("command", commands, *args, **options)

    def on_fullmatch(self, commands, *args, **options):
        return self.register("fullmatch", commands, *args, **options)

    def on_prefix(self, commands, *args, **options):
        return self.register("prefix", commands, *args, **options)

    def on_suffix(self, commands, *args, **options):
        return self.register("suffix", commands, *args, **options)

    def on_regex(self, commands, *args, **options):
        return self.register("regex", commands, *args, **options)

    def on_keyword(self, commands, *args, **options):
        return self.register("keyword", commands, *args, **options)

    def on_file(self, file_type, *args, **options):
        return self.register("file", file_type, *args, **options)

    def on_message(self, unique_id=None, *args, **options):
        return self.register(
            "message", unique_id or str(uuid.uuid4()), *args, **options
        )

    def on_meta(self, event_name, block=False, aside=None):
        return self.register("meta", event_name, block=block, prefix=False, aside=aside)


class Plugins:
    def __new__(cls, name="", *args, **kwargs):
        if name is None:
            raise ValueError("Plugins.name is None!")
        if _service_store is not None and name in _plugin_settings:
            positional = dict(
                zip(
                    (
                        "pm",
                        "priority",
                        "enabled",
                        "area",
                        "black_list",
                        "white_list",
                        "bot_white_list",
                        "bot_black_list",
                    ),
                    args,
                )
            )
            check_redeclaration(_plugin_settings[name], {**positional, **kwargs})
            return _plugin_settings[name]
        return super().__new__(cls)

    def __init__(
        self,
        name="",
        pm=6,
        priority=5,
        enabled=True,
        area="SV",
        black_list=(),
        white_list=(),
        bot_white_list=(),
        bot_black_list=(),
        sv=None,
        prefix=(),
        force_prefix=(),
        disable_force_prefix=False,
        allow_empty_prefix=None,
        force=False,
        alias=(),
        **options,
    ):
        if name != _plugin or options:
            raise Unsupported("unsupported plugin configuration")
        if getattr(self, "_initialized", False):
            return
        self.name = name
        self.area = area
        self.pm = pm
        self.priority = priority
        self.enabled = enabled
        self.prefix = [prefix] if isinstance(prefix, str) else list(prefix)
        self.force_prefix = list(force_prefix)
        self.disable_force_prefix = disable_force_prefix
        self.alias = list(alias)
        self.sv = {}
        self.white_list, self.black_list = list(white_list), list(black_list)
        self.bot_white_list, self.bot_black_list = (
            list(bot_white_list),
            list(bot_black_list),
        )
        self.allow_empty_prefix = (
            not any((*self.prefix, *self.force_prefix))
            if allow_empty_prefix is None
            else allow_empty_prefix
        )
        self._restrictions = {
            "pm": pm,
            "area": area,
            "black_list": tuple(black_list),
            "white_list": tuple(white_list),
            "bot_white_list": tuple(bot_white_list),
            "bot_black_list": tuple(bot_black_list),
        }
        self._acl_blocked = bool(
            black_list or white_list or bot_black_list or bot_white_list
        )
        prefixes = [p for p in self.prefix if p]
        if not disable_force_prefix:
            prefixes.extend(self.force_prefix)
        self.prefixes = tuple(dict.fromkeys(prefixes))
        if self.allow_empty_prefix and "" not in self.prefixes:
            self.prefixes += ("",)
        if _service_store is not None:
            _service_store.restore("plugin", self)
        _plugin_settings[name] = self
        self._initialized = True

    def set(self, is_lazy=True, **changes):
        if _service_store is None or _plugin_settings.get(self.name) is not self:
            raise Unsupported("plugin management requires the active package")
        _service_store.update("plugin", self, changes, is_lazy)


class MessageSegment:
    def __add__(self, other):
        return [self, other]

    @staticmethod
    def text(content):
        return Message("text", content)

    @staticmethod
    def image(img):
        from PIL import Image

        if isinstance(img, Image.Image):
            data = _encode_image(img.convert("RGB"))
        elif isinstance(img, str) and img.startswith("http"):
            return Message("image", "link://" + img)
        elif isinstance(img, str) and img.startswith(("file://", "base64://")):
            return Message("image", img)
        else:
            data = _media_bytes(img)
        return Message("image", "base64://" + base64.b64encode(data).decode("ascii"))

    @staticmethod
    def node(content_list):
        items = []
        for part in content_list:
            if isinstance(part, Message):
                items.append(part)
            elif isinstance(part, bytes) or (
                isinstance(part, str)
                and part.startswith(("http", "base64://", "file://"))
            ):
                items.append(MessageSegment.image(part))
            elif not isinstance(part, (bytearray, memoryview)):
                items.append(MessageSegment.text(part))
        return Message("node", items)

    @staticmethod
    def file(content, file_name):
        if isinstance(content, str) and content.startswith("http"):
            return Message("file", f"{file_name}|link://{content}")
        return Message("file", file_payload(_media_bytes(content), file_name))

    @staticmethod
    def buttons(buttons=None):
        import msgspec

        return Message("buttons", msgspec.to_builtins(process_buttons(buttons)))

    @staticmethod
    def template_buttons(template_id):
        return Message("template_buttons", template_id)

    @staticmethod
    def markdown(content, buttons=None):
        parts = [Message("markdown", content)]
        if buttons:
            parts.append(MessageSegment.buttons(buttons))
        return parts

    @staticmethod
    def template_markdown(template_id, para, buttons=None):
        parts = [
            Message("template_markdown", {"template_id": template_id, "para": para})
        ]
        if buttons:
            parts.append(MessageSegment.buttons(buttons))
        return parts

    @staticmethod
    def card(content, card_type="tuwen"):
        return [Message("card", {"type": card_type, "content": content})]

    @staticmethod
    def image_size(size):
        return Message("image_size", size)

    @staticmethod
    def at(user):
        return Message("at", user)

    @staticmethod
    def log(type, content):
        return Message(f"log_{type}", content)

    @staticmethod
    def record(content):
        return Message("record", _media_uri(content))

    @staticmethod
    def video(content):
        return Message("video", _media_uri(content))


def process_buttons(buttons):
    if not buttons:
        return buttons

    def prepare(button):
        if button.prefix and not button._edited:
            button.data = button.prefix + button.data
            button._edited = True
        return button

    first = buttons[0]
    if isinstance(first, list) and (not first or isinstance(first[0], Button)):
        return [[prepare(item) for item in row] for row in buttons]
    return [prepare(item) for item in buttons]


# Log frames belong to the adapter, not to a chat recipient. Keep a bounded,
# generation-local channel. The management inventory exposes only level counts.
_adapter_frames = deque(maxlen=32)
_adapter_counts = Counter()


class _BotLog:
    @staticmethod
    def emit(level, message):
        if not isinstance(message, str):
            raise TypeError("expected a log message")
        _adapter_frames.append((level, message[:1024]))
        _adapter_counts[level] += 1

    async def info(self, msg):
        self.emit("INFO", msg)

    async def warning(self, msg):
        self.emit("WARNING", msg)

    async def error(self, msg):
        self.emit("ERROR", msg)

    async def success(self, msg):
        self.emit("SUCCESS", msg)


def adapter_log_counts():
    return dict(_adapter_counts)


def _image_bytes(value):
    if isinstance(value, str) and value.startswith("base64://"):
        encoded = value[9:]
        if len(encoded) > ((MAX_INPUT_IMAGE_BYTES + 2) // 3) * 4:
            raise UnsupportedMessage("image exceeds response limit")
        try:
            value = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise UnsupportedMessage("invalid encoded image") from exc
    if isinstance(value, PathLike):
        path = Path(value).resolve()
        if not any(
            root and path.is_relative_to(root) for root in (_source, _data_root)
        ):
            raise UnsupportedMessage("image path outside plugin directories")
        with path.open("rb") as stream:
            value = stream.read(MAX_INPUT_IMAGE_BYTES + 1)
    if not isinstance(value, bytes):
        from PIL import Image

        if not isinstance(value, Image.Image):
            raise UnsupportedMessage("unsupported image source")
        value = _encode_image(value)
    if not 0 < len(value) <= MAX_INPUT_IMAGE_BYTES:
        raise UnsupportedMessage("invalid image size")
    if not value.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")):
        raise UnsupportedMessage("expected an encoded PNG or JPEG")
    return value


def _encode_image(image):
    if not 0 < image.width * image.height <= MAX_INPUT_PIXELS:
        raise UnsupportedMessage("invalid image dimensions")
    stream = BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def _media_bytes(value):
    if isinstance(value, (str, PathLike)):
        path = Path(value).resolve()
        if not any(
            root and path.is_relative_to(root) for root in (_source, _data_root)
        ):
            raise UnsupportedMessage("media path outside plugin directories")
        with path.open("rb") as stream:
            value = stream.read(MAX_INPUT_IMAGE_BYTES + 1)
    if (
        not isinstance(value, (bytes, bytearray, memoryview))
        or len(value) > MAX_INPUT_IMAGE_BYTES
    ):
        raise UnsupportedMessage("unsupported media payload")
    return bytes(value)


def _media_uri(value):
    if isinstance(value, str) and value.startswith(("base64://", "file://")):
        return value
    return "base64://" + base64.b64encode(_media_bytes(value)).decode("ascii")


def convert_img_sync(img, is_base64=False):
    from PIL import Image

    if not isinstance(img, Image.Image):
        return "base64://" + base64.b64encode(_media_bytes(img)).decode("ascii")
    if not 0 < img.width * img.height <= MAX_INPUT_PIXELS:
        raise UnsupportedMessage("invalid image dimensions")
    stream = BytesIO()
    if img.format == "GIF":
        img.save(stream, format="GIF")
    else:
        img.convert("RGB").save(stream, format="JPEG", quality=85)
    data = _media_bytes(stream.getvalue())
    return "base64://" + base64.b64encode(data).decode("ascii") if is_base64 else data


async def convert_img(img, is_base64=False):
    return await asyncio.to_thread(convert_img_sync, img, is_base64)


def worker_font(source, package_name, size):
    """Load a CJK face available to this isolated renderer."""
    from PIL import ImageFont

    repository = Path(source) / package_name
    candidates = [
        *sorted((repository / package_name / "utils" / "fonts").glob("*origin.ttf")),
        *sorted((repository / "utils" / "fonts").glob("*origin.ttf")),
        "msyh.ttc",
        "NotoSansCJK-Regular.ttc",
    ]
    for face in candidates:
        try:
            return ImageFont.truetype(str(face), size)
        except OSError:
            continue
    raise UnsupportedMessage("no CJK font available")


class Bot:
    get_lang = bot_get_lang
    t = bot_t

    def __init__(self, bot=None, ev=None, *, target_transport=None, interaction=None):
        self._lang = None
        self.ev = ev if ev is not None else Event()
        self.bot = bot
        self.bot_id = self.ev.bot_id
        self.bot_self_id = self.ev.bot_self_id
        self.uid = self.ev.user_id or "0"
        self.bid = self.bot_id or "0"
        self.temp_gid = (
            self.uid if self.ev.user_type == "direct" else self.ev.group_id or "0"
        )
        self.session_id = f"{self.bid}%%%{self.temp_gid}%%%{self.uid}"
        self._target_transport = target_transport
        self._interaction = interaction
        self.messages = []
        self.logger = bot.logger if bot is not None else _BotLog()
        self.image_size = 0
        self.text_size = 0

    async def target_send(
        self,
        message,
        target_type,
        target_id,
        at_sender=False,
        sender_id="",
        send_source_group=None,
        wait_recall=False,
    ):
        if self._target_transport is None:
            raise UnsupportedMessage("host target delivery unavailable")
        if at_sender or sender_id or send_source_group or wait_recall:
            raise UnsupportedMessage("target metadata or recall unsupported")
        await self._target_transport.send(target_type, target_id, message)

    async def receive_resp(
        self,
        reply=None,
        option_list=None,
        unsuported_platform=False,
        is_mutiply=False,
        is_recive=True,
        timeout=60,
        sep="\n",
        command_tips="请输入以下命令之一:",
        command_start_text="",
        at_sender=False,
    ):
        if self._interaction is None:
            raise UnsupportedMessage("host interactive replies unavailable")
        if (
            option_list
            or at_sender
            or (reply is not None and not isinstance(reply, str))
        ):
            raise UnsupportedMessage("interactive port accepts plain prompts only")
        if reply:
            await self._interaction.prompt(reply)
        if is_mutiply or is_recive:
            try:
                return await asyncio.wait_for(
                    self._interaction.receive(is_mutiply), timeout
                )
            except TimeoutError:
                if is_mutiply:
                    raise
                return None

    async def receive_mutiply_resp(
        self,
        reply=None,
        option_list=None,
        unsuported_platform=False,
        timeout=60,
        sep="\n",
        command_tips="请输入以下命令之一:",
        command_start_text="",
    ):
        return await self.receive_resp(
            reply,
            option_list,
            unsuported_platform,
            True,
            True,
            timeout,
            sep,
            command_tips,
            command_start_text,
        )

    def _append(self, value):
        if isinstance(value, (list, tuple)):
            for part in value:
                self._append(part)
            return
        if isinstance(value, Message):
            if value.type == "node":
                self._append(value.data)
                return
            kind, value = value.type, value.data
        elif isinstance(value, str) and not value.startswith("base64://"):
            kind = "text"
        else:
            kind = "image"

        if len(self.messages) >= _MAX_PARTS:
            raise UnsupportedMessage("too many message parts")
        if kind == "text" and isinstance(value, str):
            if not value.strip():
                return
            self.text_size += len(value)
            if self.text_size > _MAX_TEXT:
                raise UnsupportedMessage("text exceeds response limit")
            self.messages.append({"text": value})
        elif kind == "image":
            raw = _image_bytes(value)
            self.image_size += len(raw)
            if self.image_size > MAX_INPUT_IMAGE_BYTES:
                raise UnsupportedMessage("images exceed response limit")
            self.messages.append({"image": base64.b64encode(raw).decode("ascii")})
        else:
            raise UnsupportedMessage("unsupported message segment")

    async def send(self, message, at_sender=False):
        previous = len(self.messages), self.text_size, self.image_size
        try:
            self._append(message)
        except UnsupportedMessage:
            count, self.text_size, self.image_size = previous
            del self.messages[count:]
            raise

    async def send_option(
        self,
        reply=None,
        option_list=None,
        unsuported_platform=None,
        sep="\n",
        command_tips="请输入以下命令之一:",
        command_start_text="",
        at_sender=False,
    ):
        if reply is None and option_list:
            reply = "请在60秒内做出选择..."
        if reply is not None:
            await self.send(reply, at_sender)
        # An explicit False requests only the native reply on a text-only adapter.
        if option_list and unsuported_platform is not False:
            choices = []
            for row in option_list:
                for b in row if isinstance(row, (tuple, list)) else (row,):
                    if isinstance(b, Button):
                        data = b.data
                        prefix = b.prefix or _prefix
                    elif isinstance(b, str):
                        data, prefix = b, _prefix
                    else:
                        raise UnsupportedMessage("unsupported button")
                    if unsuported_platform is True:
                        data = command_start_text + data
                    elif data and not data.startswith((prefix, "https://", "http://")):
                        data = prefix + data
                    if data:
                        choices.append(data)
            if choices:
                tips = f"\n{command_tips}\n" if unsuported_platform else ""
                self._append(tips + sep.join(choices))


def get_res_path(_path=None):
    name = _path
    root = _data_root / "resources"
    if isinstance(name, Path):
        root = name if name.is_absolute() else root / name
    elif name is not None:
        parts = name if isinstance(name, (tuple, list)) else (name,)
        for part in parts:
            if (
                not isinstance(part, str)
                or not part
                or Path(part).anchor
                or ".." in Path(part).parts
            ):
                raise ValueError("invalid resource directory")
            root /= part
    if not root.resolve().is_relative_to(_data_root / "resources"):
        raise ValueError("resource directory outside plugin resources")
    root.mkdir(exist_ok=True, parents=True)
    if getattr(sys.modules.get("game_profile"), "PUBLIC_RESOURCES_ONLY", False):
        from resource_demand import ResourcePath

        return ResourcePath(root)
    return root


def plugin_settings(name):
    if name not in _plugin_settings:
        raise ValueError(f"插件{name}不存在!")
    return _plugin_settings[name]


def get_plugin_available_prefix(name):
    if _service_store is None and name == _plugin and name not in _plugin_settings:
        return _prefix
    plugin = plugin_settings(name)
    if not plugin.disable_force_prefix and plugin.force_prefix:
        return plugin.force_prefix[0]
    if plugin.disable_force_prefix and plugin.prefix:
        return plugin.prefix[0]
    return ""


def flush_loaded_service_settings():
    if _service_store is not None:
        _service_store._after_load()


def install(plugin, prefix, data_root, *, source_root=None, runtime_mode=None):
    global _plugin, _prefix, _data_root, _source, _service_store
    _routes.clear()
    _adapter_frames.clear()
    _adapter_counts.clear()
    _plugin_settings.clear()
    _services.clear()
    _plugin, _prefix = plugin, prefix
    _data_root = Path(data_root).resolve()
    if runtime_mode is None:
        runtime_mode = getattr(
            sys.modules.get("game_profile"), "RUNTIME_MODE", "package"
        )
    _service_store = select_service_profile(_data_root, plugin, runtime_mode == "package")
    _source = (
        Path(source_root).resolve()
        if source_root
        else (Path.cwd() / "source").resolve()
    )
    core = package("gsuid_core")
    package("gsuid_core.version").__version__ = core.__version__ = "0.12.2"
    install_app_life(package)
    triggers = package("gsuid_core.trigger")
    for name in (
        "Trigger",
        "dispatch_sort_key",
        "registry_version",
        "bump_registry_version",
        "gap_chars",
    ):
        setattr(triggers, name, getattr(plugin_trigger, name))
    sv = package("gsuid_core.sv")
    sv.SVList = SVList
    sv.SL = SVList()
    sv.SL.plugins = _plugin_settings
    sv.SL.lst = _services
    sv.SV = SV
    sv.Plugins = Plugins
    sv.get_plugin_available_prefix = get_plugin_available_prefix
    sv.get_plugin_prefixs = lambda name: plugin_settings(name).prefix
    sv.get_plugin_prefix = lambda name: sv.get_plugin_prefixs(name)[0]
    sv.get_plugin_force_prefixs = lambda name: plugin_settings(name).force_prefix
    sv.get_plugin_force_prefix = lambda name: sv.get_plugin_force_prefixs(name)[0]
    messages = package("gsuid_core.message_models")
    messages.Button = Button
    messages.ButtonType = ButtonType
    messages.ButtonList = ButtonList
    messages.Message = Message
    from msgspec import json as msgjson

    bot = package("gsuid_core.bot")
    bot.Bot = Bot
    bot.msgjson = msgjson
    models = package("gsuid_core.models")
    models.Event = Event
    models.Message = Message
    models.MessageReceive = MessageReceive
    models.MessageSend = MessageSend
    segments = package("gsuid_core.segment")
    segments.MessageSegment = MessageSegment
    segments.process_buttons = process_buttons
    core_config = install_core_config(package("gsuid_core.config"), _data_root, _service_store)
    if _service_store is not None:
        sv.config_plugins = _service_store.get_all()
    install_ai(package)
    install_logger(package("gsuid_core.logger"), core_config, source_root=_source)
    install_data_store(package("gsuid_core.data_store"), get_res_path)
    install_i18n(package("gsuid_core.i18n"), source_root=_source)
    package("gsuid_core.utils")
    install_qrlogin(package, _data_root)
    install_status(package)
    downloads = install_downloads(package, _data_root)
    install_minigg(package, _data_root)
    install_public_api()
    install_pool(package)
    install_settings(package, _data_root, plugin)
    package("gsuid_core.utils.image")
    profile = sys.modules.get("game_profile")
    published = PublishedImages(
        _source,
        plugin,
        verified_urls=profile.STATIC_RESOURCE_URLS
        if getattr(profile, "PUBLIC_RESOURCES_ONLY", False)
        and profile.PACKAGE == plugin
        else None,
    )
    install_image_fetch(package, _data_root, published=published)
    tools = install_image_tools(package, _source, _data_root, fetch=published.fetch)
    tools.get_div = plugin_text_image.get_div
    tools.draw_center_text_by_line = plugin_text_image.draw_center_text_by_line
    package(
        "gsuid_core.utils.image.mys",
        Path(__file__).parent / "plugin_public_ports" / "bbs",
    )
    conversion = package("gsuid_core.utils.image.convert")
    conversion.convert_img = convert_img
    conversion.convert_img_sync = convert_img_sync
    for name in (
        "str_lenth",
        "get_str_size",
        "get_height",
        "text2pic",
        "number_to_chinese",
    ):
        setattr(conversion, name, getattr(plugin_text_image, name))
    install_help(package, _data_root, source_root=_source)
    return downloads


def _authorized(subject, event, mapped):
    if not subject.enabled or event.user_pm > subject.pm:
        return False
    restrictions = subject._restrictions
    if subject._acl_blocked and not mapped:
        return False
    actor, room = event.user_id, event.group_id
    denied = restrictions["black_list"]
    allowed = restrictions["white_list"]
    if actor in denied or room in denied:
        return False
    if allowed and allowed != ("",) and actor not in allowed and room not in allowed:
        return False
    bots = {b for b in restrictions.get("bot_white_list", ()) if b}
    excluded = {b for b in restrictions.get("bot_black_list", ()) if b}
    return event.bot_self_id not in excluded and (not bots or event.bot_self_id in bots)


def _match(route, event, mapped):
    kind = event.user_type.upper()
    settings = _plugin_settings.get(_plugin)
    if settings and (
        not _authorized(settings, event, mapped)
        or settings.area not in ("SV", "ALL", kind)
    ):
        return None
    if not _authorized(route.sv, event, mapped):
        return None
    if route.sv.area not in ("ALL", kind) and not (settings and settings.area == "ALL"):
        return None
    if (event.meta_event_type is not None) != (route.type == "meta"):
        return False
    return route.check_command(event)


async def dispatch(
    text,
    *,
    actor_id="",
    group_id="",
    message_id="",
    adapter_id="astrbot",
    conversation_id="",
    conversation_kind="",
):
    if conversation_kind == "group" and not group_id:
        group_id = conversation_id
    return await dispatch_event(
        Event(
            text=text,
            raw_text=text,
            content=[Message("text", text)],
            user_id=actor_id,
            group_id=group_id,
            user_type="group" if group_id else conversation_kind,
            msg_id=message_id,
            bot_id=adapter_id,
            real_bot_id=adapter_id,
            WS_BOT_ID="",
        ),
        mapped=False,
    )


async def dispatch_event(incoming, *, mapped):
    """Dispatch a decoded adapter event. Only trusted adapter inputs can supply roles."""
    matched = [route for route in _routes if _match(route, incoming, mapped)]
    listeners = [route for route in matched if route.type == "message"]
    commands = sorted(
        (route for route in matched if route.type != "message"),
        key=lambda r: plugin_trigger.dispatch_sort_key(r, r.sv.priority),
    )
    parts = []
    bytes_used = text_used = 0
    first_error = None
    for route in (*listeners, *commands):
        event = await route.get_command(deepcopy(incoming))
        event.task_id = str(uuid.uuid4())
        bot = Bot(ev=event)
        bot.messages = parts
        bot.text_size, bot.image_size = text_used, bytes_used
        try:
            await route.func(bot, event)
        except (UnsupportedMessage, Unsupported, ImportError, NotImplementedError):
            raise
        except Exception as exc:
            if first_error is None:
                first_error = exc
            _BotLog.emit("ERROR", type(exc).__name__)
        text_used, bytes_used = bot.text_size, bot.image_size
        if route.block and route.type != "message":
            break
    if not parts:
        if first_error is not None:
            raise first_error
        if not commands:
            raise NoMatch
        raise UnsupportedMessage("handler produced no deliverable content")
    return parts
