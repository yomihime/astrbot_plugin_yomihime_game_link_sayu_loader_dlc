"""Worker-local host ports for upstream chat services."""

import base64
import binascii
import asyncio
import logging
import re
import sys
from collections import Counter, deque
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from io import BytesIO
from os import PathLike
from pathlib import Path
from types import ModuleType, SimpleNamespace

from plugin_settings import MODELS, configuration_type
from service_settings import ServiceSettings
from declarations import Unsupported
from target_transport import UnsupportedMessage, file_payload


class NoMatch(LookupError):
    """No available service accepts this command and conversation."""


@dataclass
class Route:
    service: object
    mode: str
    keyword: str
    pattern: object
    handler: object
    prefix: str
    block: bool


_routes = []
_plugin_settings = {}
_services = {}
_service_store = None
_prefix = ""
_plugin = ""
_data_root = None
_source = None
_ranks = {
    "fullmatch": 0,
    "command": 1,
    "prefix": 2,
    "suffix": 2,
    "regex": 3,
    "keyword": 4,
}
MAX_INPUT_IMAGE_BYTES = 16 * 1024 * 1024
MAX_INPUT_PIXELS = 12_000_000
_MAX_TEXT = 4096
_MAX_PARTS = 32
_GAPS = " \t\u3000\u00a0"
_GAP_PATTERN = "[" + _GAPS + "]"


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
        "services": len({id(route.service) for route in _routes}),
        "handlers": len({route.handler for route in _routes}),
        "triggers": len(_routes),
    }


def registered_triggers():
    """List registrations without invoking game code or inspecting any user data."""
    return [
        {
            "service": route.service.name,
            "handler": route.handler.__module__ + "." + route.handler.__qualname__,
            "mode": route.mode,
            "pattern": route.keyword,
            "prefix": bool(route.prefix),
            "area": route.service.area,
        }
        for route in _routes
    ]


def _literal_pattern(keyword):
    # Gaps at Han/ASCII boundaries are optional, spaces within a word are required.
    pieces = []
    previous = ""
    for char in keyword:
        if char in _GAPS:
            if not previous or previous not in _GAPS:
                pieces.append(_GAP_PATTERN + "+")
        else:
            if previous and previous not in _GAPS:
                han = "\u3400" <= char <= "\u9fff"
                prev_han = "\u3400" <= previous <= "\u9fff"
                if (han and previous.isascii() and previous.isalnum()) or (
                    prev_han and char.isascii() and char.isalnum()
                ):
                    pieces.append(_GAP_PATTERN + "*")
            pieces.append(re.escape(char))
        previous = char
    return "".join(pieces)


def check_redeclaration(instance, options):
    for key, declared in instance._restrictions.items():
        if key in options:
            value = tuple(options[key]) if key.endswith("_list") else options[key]
            if value != declared:
                raise Unsupported("conflicting permission declarations for one name")


class SV:
    def __new__(cls, name="", **kwargs):
        if _service_store is not None and name in _services:
            check_redeclaration(_services[name], kwargs)
            return _services[name]
        return super().__new__(cls)

    def __init__(
        self,
        name="",
        *,
        pm=6,
        priority=5,
        area="ALL",
        enabled=True,
        black_list=(),
        white_list=(),
        **options,
    ):
        if options:
            raise Unsupported("unsupported service options")
        if getattr(self, "_initialized", False):
            return
        self.name = name
        self.pm = pm
        self.priority = priority
        self.area = area
        self.enabled = enabled
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
        self._initialized = True

    def set(self, is_lazy=True, **changes):
        if _service_store is None or _services.get(self.name) is not self:
            raise Unsupported("service management requires the active package")
        _service_store.update("service", self, changes)

    def enable(self):
        self.set(enabled=True)

    def disable(self):
        self.set(enabled=False)

    def register(
        self,
        mode,
        commands,
        *,
        block=False,
        prefix=True,
        to_me=False,
        to_ai="",
        **options,
    ):
        def decorate(handler):
            if to_me or options or (_service_store is not None and to_ai):
                raise Unsupported("unsupported trigger options")
            if _service_store is None and (not self.enabled or self._acl_blocked):
                raise Unsupported("restricted service")
            words = (commands,) if isinstance(commands, str) else commands
            for word in dict.fromkeys(words):
                if not isinstance(word, str) or not word:
                    raise ValueError("invalid trigger keyword")
                expression = (
                    word
                    if mode == "regex"
                    else re.escape(word)
                    if mode == "keyword"
                    else _literal_pattern(word)
                )
                if mode == "suffix":
                    expression += r"\Z"
                settings = _plugin_settings.get(_plugin)
                prefixes = settings.prefixes if settings else (_prefix,)
                for literal_prefix in prefixes if prefix and prefixes else ("",):
                    _routes.append(
                        Route(
                            self,
                            mode,
                            word,
                            re.compile(expression),
                            handler,
                            literal_prefix,
                            block,
                        )
                    )
            return handler

        return decorate

    def on_command(self, commands, **options):
        return self.register("command", commands, **options)

    def on_fullmatch(self, commands, **options):
        return self.register("fullmatch", commands, **options)

    def on_prefix(self, commands, **options):
        return self.register("prefix", commands, **options)

    def on_suffix(self, commands, **options):
        return self.register("suffix", commands, **options)

    def on_regex(self, commands, **options):
        return self.register("regex", commands, **options)

    def on_keyword(self, commands, **options):
        return self.register("keyword", commands, **options)

    def on_file(self, commands, **options):
        # This transport carries text input only.
        raise Unsupported("file input is unavailable")


class Plugins:
    def __new__(cls, name="", **kwargs):
        if _service_store is not None and name in _plugin_settings:
            check_redeclaration(_plugin_settings[name], kwargs)
            return _plugin_settings[name]
        return super().__new__(cls)

    def __init__(
        self,
        name="",
        *,
        pm=6,
        priority=5,
        enabled=True,
        area="SV",
        prefix=(),
        force_prefix=(),
        allow_empty_prefix=None,
        alias=(),
        black_list=(),
        white_list=(),
        bot_black_list=(),
        bot_white_list=(),
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
        prefixes = (prefix,) if isinstance(prefix, str) else tuple(prefix)
        self.prefixes = tuple(dict.fromkeys((*prefixes, *force_prefix)))
        if allow_empty_prefix or (allow_empty_prefix is None and not self.prefixes):
            self.prefixes += ("",)
        if _service_store is not None:
            _service_store.restore("plugin", self)
        _plugin_settings[name] = self
        self._initialized = True

    def set(self, is_lazy=True, **changes):
        if _service_store is None or _plugin_settings.get(self.name) is not self:
            raise Unsupported("plugin management requires the active package")
        _service_store.update("plugin", self, changes)


class Event(SimpleNamespace):
    pass


@dataclass
class Message:
    type: str
    data: object


class MessageSegment:
    @staticmethod
    def text(content):
        return Message("text", content)

    @staticmethod
    def image(img):
        return Message("image", img)

    @staticmethod
    def node(content_list):
        return Message("node", content_list)

    @staticmethod
    def file(content, file_name):
        return Message("file", file_payload(content, file_name))


@dataclass
class Button:
    text: str
    data: str
    action: int = -1
    prefix: str = ""


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


async def convert_img(img, *, is_base64=False):
    data = _image_bytes(img)
    if is_base64:
        return "base64://" + base64.b64encode(data).decode("ascii")
    return data


def _help_rows(sections, prefix, pm):
    for category, section in sections.items():
        entries = [
            item for item in section["data"] if pm <= 1 or not item.get("need_admin")
        ]
        if not entries:
            continue
        yield True, category
        if section.get("desc"):
            yield False, section["desc"]
        for item in entries:
            yield False, f"{item['name']}   {item['desc']}"
            if item.get("eg"):
                yield False, f"  {prefix}{item['eg']}"


def worker_font(source, package_name, size):
    """Load a CJK face available to this isolated renderer."""
    from PIL import ImageFont

    directory = Path(source) / package_name / "utils" / "fonts"
    candidates = [
        *sorted(directory.glob("*origin.ttf")),
        "msyh.ttc",
        "NotoSansCJK-Regular.ttc",
    ]
    for face in candidates:
        try:
            return ImageFont.truetype(str(face), size)
        except OSError:
            continue
    raise UnsupportedMessage("no CJK font available")


async def get_new_help(
    *,
    plugin_name,
    plugin_info,
    plugin_help,
    plugin_prefix,
    plugin_icon=None,
    banner_bg=None,
    banner_sub_text="",
    pm=6,
    **style,
):
    """Render the upstream help schema with its labels, examples and branding."""
    from PIL import Image, ImageDraw, ImageOps
    from textwrap import wrap

    width = 760
    rows = []
    for heading, line in _help_rows(plugin_help, plugin_prefix, pm):
        for part in wrap(str(line), 32, replace_whitespace=False):
            rows.append((heading, part))
    height = 140 + len(rows) * 31
    if width * height > MAX_INPUT_PIXELS:
        raise UnsupportedMessage("help table exceeds image dimensions")
    canvas = Image.new("RGB", (width, height), (30, 34, 43))
    if banner_bg:
        banner = ImageOps.fit(banner_bg.convert("RGB"), (width, 110))
        canvas.paste(Image.blend(banner, Image.new("RGB", banner.size), 0.6))
    if plugin_icon:
        icon = plugin_icon.convert("RGBA")
        icon.thumbnail((80, 80))
        canvas.paste(icon, (15, 14), icon)
    font = worker_font(_source, _plugin, 20)
    draw = ImageDraw.Draw(canvas)
    draw.text((112, 20), "  ".join((plugin_name, *plugin_info.keys())), font=font)
    draw.text((112, 57), banner_sub_text, font=font)
    for index, (heading, line) in enumerate(rows):
        draw.text(
            (18 if heading else 36, 120 + index * 31),
            line,
            font=font,
            fill=(135, 223, 255) if heading else (242, 243, 245),
        )
    return _encode_image(canvas)


class Bot:
    def __init__(self, *, target_transport=None, interaction=None):
        self._target_transport = target_transport
        self._interaction = interaction
        self.messages = []
        self.logger = _BotLog()
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

    async def send_option(self, value, options):
        await self.send(value)
        # WebChat's text/image transport exposes the commands as plain text.
        if options:
            choices = []
            for row in options:
                for b in row if isinstance(row, (tuple, list)) else (row,):
                    if not isinstance(b, Button):
                        raise UnsupportedMessage("unsupported button")
                    prefix = b.prefix or _prefix
                    data = b.data
                    if data and not data.startswith((prefix, "https://", "http://")):
                        data = prefix + data
                    if data:
                        choices.append(data)
            self._append("\n".join(choices))


def get_res_path(name=None):
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
    return root


def get_plugin_available_prefix(name):
    if name != _plugin:
        raise KeyError(name)
    return _prefix


def to_thread(function):
    import asyncio
    import inspect

    @wraps(function)
    async def run(*args, **kwargs):
        if inspect.iscoroutinefunction(function):
            return await asyncio.to_thread(
                lambda: asyncio.run(function(*args, **kwargs))
            )
        return await asyncio.to_thread(function, *args, **kwargs)

    return run


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
    _service_store = ServiceSettings(_data_root) if runtime_mode == "package" else None
    _source = (
        Path(source_root).resolve()
        if source_root
        else (Path.cwd() / "source").resolve()
    )
    package("gsuid_core")
    sv = package("gsuid_core.sv")
    sv.SL = SimpleNamespace(plugins=_plugin_settings, lst=_services)
    sv.SV = SV
    sv.Plugins = Plugins
    sv.get_plugin_available_prefix = get_plugin_available_prefix
    messages = package("gsuid_core.message_models")
    messages.Button = Button
    messages.Message = Message
    package("gsuid_core.bot").Bot = Bot
    models = package("gsuid_core.models")
    models.Event = Event
    models.Message = Message
    package("gsuid_core.segment").MessageSegment = MessageSegment
    package("gsuid_core.logger").logger = logging.getLogger("sayu")
    package("gsuid_core.data_store").get_res_path = get_res_path
    package("gsuid_core.i18n").t = (
        lambda key, **fields: key.format_map(fields) if fields else key
    )
    package("gsuid_core.utils")
    pool = package("gsuid_core.pool")
    pool.to_thread = pool.run_in_thread_pool = to_thread
    package("gsuid_core.utils.plugins_config")
    config = package("gsuid_core.utils.plugins_config.models")
    for name, model in MODELS.items():
        setattr(config, name, model)
    package(
        "gsuid_core.utils.plugins_config.gs_config"
    ).StringConfig = configuration_type(_data_root)
    package("gsuid_core.utils.image")
    package("gsuid_core.utils.image.convert").convert_img = convert_img
    package("gsuid_core.help")
    package("gsuid_core.help.model").PluginHelp = dict
    package("gsuid_core.help.draw_new_plugin_help").get_new_help = get_new_help


def _match(route, raw, kind):
    service = route.service
    settings = _plugin_settings.get(_plugin)
    if settings and (
        not settings.enabled
        or settings.pm < 6
        or settings._acl_blocked
        or settings.area not in ("SV", "ALL", kind.upper())
    ):
        return None
    if (
        not service.enabled
        or service._acl_blocked
        or service.pm < 6
        or service.area not in ("ALL", kind.upper())
    ):
        return None
    text = raw.strip(_GAPS)
    if not text.startswith(route.prefix):
        return None
    body = text[len(route.prefix) :].lstrip(_GAPS)

    pattern = route.pattern
    if route.mode == "fullmatch":
        match = pattern.fullmatch(body)
    elif route.mode == "keyword":
        match = pattern.search(text)
    elif route.mode in ("suffix", "regex"):
        match = pattern.search(body)
    else:
        match = pattern.match(body)
    if match is None:
        return None
    if route.mode == "regex":
        groups = match.groups()
        return dict(
            command="|".join(v or "" for v in groups),
            text="|".join(v or "" for v in pattern.split(raw)),
            regex_dict=match.groupdict(),
            regex_group=groups,
        )
    if route.mode in ("suffix", "keyword"):
        head = re.match(_literal_pattern(route.keyword), body)
        remainder = body[head.end() :] if head else body.replace(route.keyword, "", 1)
    else:
        remainder = body[match.end() :]
    remainder = remainder.strip(_GAPS)
    if route.mode in ("prefix", "suffix") and not remainder:
        return None
    return dict(text=remainder, command=route.keyword, regex_group=(), regex_dict={})


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
    ordered = sorted(
        _routes,
        key=lambda r: (
            not r.prefix,
            r.service.priority,
            _ranks[r.mode],
            -len(r.keyword)
            if r.mode in {"fullmatch", "command", "prefix", "suffix"}
            else 0,
        ),
    )
    bot = Bot()
    invoked = False
    for route in ordered:
        kind = "group" if group_id else conversation_kind
        fields = _match(route, text, kind)
        if fields is None:
            continue
        event = Event(
            **fields,
            raw_text=text,
            user_id=actor_id,
            group_id=group_id,
            user_type=kind,
            user_pm=6,
            msg_id=message_id,
            bot_id=adapter_id,
            real_bot_id=adapter_id,
            bot_self_id="",
            WS_BOT_ID="",
            at=None,
            sender={},
            image=None,
            reply=None,
            file=None,
            file_type=None,
            file_name=None,
        )
        await route.handler(bot, event)
        invoked = True
        if route.block:
            break
    if not invoked:
        raise NoMatch
    if not bot.messages:
        raise UnsupportedMessage("handler produced no deliverable content")
    return bot.messages
