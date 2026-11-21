"""Worker-local host ports for upstream chat services."""

import base64
import binascii
import logging
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from io import BytesIO
from os import PathLike
from pathlib import Path
from types import ModuleType, SimpleNamespace


class NoMatch(LookupError):
    """No available service accepts this command and conversation."""


class UnsupportedMessage(ValueError):
    """The host cannot deliver this reply without losing content."""


@dataclass
class Route:
    service: object
    mode: str
    keyword: str
    pattern: object
    handler: object
    prefix: bool
    block: bool


_routes = []
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
            "prefix": route.prefix,
            "area": route.service.area,
        }
        for route in _routes
    ]


def _literal_pattern(keyword):
    # Gaps at Han/ASCII boundaries are optional, spaces within a word are required.
    pieces = []
    previous = ""
    for char in keyword:
        if char.isspace():
            if not previous.isspace():
                pieces.append(r"\s+")
        else:
            if previous and not previous.isspace():
                han = "\u3400" <= char <= "\u9fff"
                prev_han = "\u3400" <= previous <= "\u9fff"
                if (han and previous.isascii() and previous.isalnum()) or (
                    prev_han and char.isascii() and char.isalnum()
                ):
                    pieces.append(r"\s*")
            pieces.append(re.escape(char))
        previous = char
    return "".join(pieces)


class SV:
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
        self.name = name
        self.pm = pm
        self.priority = priority
        self.area = area
        # The bridge has no upstream group/identity ACL or aside capability.
        self.enabled = enabled and not (
            black_list or white_list or any(options.values())
        )

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
            if to_me or any(options.values()) or not self.enabled:
                return handler
            words = (commands,) if isinstance(commands, str) else commands
            for word in dict.fromkeys(words):
                if not isinstance(word, str) or not word:
                    raise ValueError("invalid trigger keyword")
                expression = word if mode == "regex" else _literal_pattern(word)
                if mode == "suffix":
                    expression += r"\Z"
                _routes.append(
                    Route(
                        self, mode, word, re.compile(expression), handler, prefix, block
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
        return lambda handler: handler


class Plugins:
    def __init__(self, name="", **options):
        self.name = name


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
    def file(name, content):
        return Message("file", content)


@dataclass
class Button:
    text: str
    data: str
    action: int = -1
    prefix: str = ""


class _BotLog:
    async def info(self, *args, **kwargs):
        # Upstream diagnostic strings can include private identifiers.
        pass

    debug = warning = error = info


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
    def __init__(self):
        self.messages = []
        self.logger = _BotLog()
        self.image_size = 0
        self.text_size = 0

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
        self._append(message)

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
    if name is not None:
        parts = name if isinstance(name, (tuple, list)) else (name,)
        for part in parts:
            if (
                not isinstance(part, str)
                or Path(part).name != part
                or part in ("", ".", "..")
            ):
                raise ValueError("invalid resource directory")
            root /= part
    if not root.resolve().is_relative_to(_data_root):
        raise ValueError("resource directory outside data root")
    root.mkdir(exist_ok=True, parents=True)
    return root


def get_plugin_available_prefix(name):
    if name != _plugin:
        raise KeyError(name)
    return _prefix


class _ConfigValue:
    def __init__(self, name, desc, data, *display_args, **display_options):
        self.name = name
        self.desc = desc
        self.data = data


class StringConfig:
    """Read defaults declared by the package without opening any host configuration."""

    def __init__(self, name, path, defaults):
        self._defaults = defaults

    def get_config(self, key):
        return self._defaults[key]


def to_thread(function):
    # A worker owns one request and is already isolated from the host event loop.
    @wraps(function)
    async def run(*args, **kwargs):
        return function(*args, **kwargs)

    return run


def install(plugin, prefix, data_root, *, source_root=None):
    global _plugin, _prefix, _data_root, _source
    _routes.clear()
    _plugin, _prefix = plugin, prefix
    _data_root = Path(data_root).resolve()
    _source = (
        Path(source_root).resolve()
        if source_root
        else (Path.cwd() / "source").resolve()
    )
    package("gsuid_core")
    sv = package("gsuid_core.sv")
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
    package("gsuid_core.pool").to_thread = to_thread
    package("gsuid_core.utils.plugins_config")
    config = package("gsuid_core.utils.plugins_config.models")
    for name in (
        "GsBoolConfig",
        "GsImageConfig",
        "GsIntConfig",
        "GsListStrConfig",
        "GsStrConfig",
    ):
        setattr(config, name, _ConfigValue)
    package("gsuid_core.utils.plugins_config.gs_config").StringConfig = StringConfig
    package("gsuid_core.utils.image")
    package("gsuid_core.utils.image.convert").convert_img = convert_img
    package("gsuid_core.help")
    package("gsuid_core.help.model").PluginHelp = dict
    package("gsuid_core.help.draw_new_plugin_help").get_new_help = get_new_help


def _match(route, raw, kind):
    service = route.service
    if service.pm < 6 or service.area not in ("ALL", kind.upper()):
        return None
    text = raw.strip()
    if route.prefix:
        if not text.startswith(_prefix):
            return None
        text = text[len(_prefix) :].lstrip()

    pattern = route.pattern
    if route.mode == "fullmatch":
        match = pattern.fullmatch(text)
    elif route.mode in ("suffix", "regex", "keyword"):
        match = pattern.search(text)
    else:
        match = pattern.match(text)
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
    remainder = (text[: match.start()] + text[match.end() :]).strip()
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
            -len(r.keyword) if r.mode != "regex" else 0,
        ),
    )
    bot = Bot()
    invoked = set()
    for route in ordered:
        if route.handler in invoked:
            continue
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
        invoked.add(route.handler)
        if route.block:
            break
    if not invoked:
        raise NoMatch
    if not bot.messages:
        raise UnsupportedMessage("handler produced no deliverable content")
    return bot.messages
