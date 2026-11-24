"""Local gsuid_core image helpers used by the three pinned plugin packages.

Pixel behavior follows GsCore 96cd7353be3100ff982bccc33e554961837c778e
(GPL-3.0, see licenses/gsuid_core-COPYING.txt). The generation owns its
source and data roots. Remote reads require a host-supplied bounded transport.
"""

import asyncio
import math
import pkgutil
import random
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

from port_errors import Unsupported

_roots = ()
_fetch = None


def _pixels(size):
    from sayu_compat import MAX_INPUT_PIXELS

    if size[0] * size[1] > MAX_INPUT_PIXELS:
        raise Unsupported("image dimensions exceed the worker limit")


def _decode(data):
    from sayu_compat import MAX_INPUT_IMAGE_BYTES

    if len(data) > MAX_INPUT_IMAGE_BYTES:
        raise Unsupported("image bytes exceed the worker limit")
    with Image.open(BytesIO(data)) as image:
        _pixels(image.size)
        return image.convert("RGBA")


def _asset(name):
    raw = pkgutil.get_data(__name__, "plugin_image_assets/" + name)
    if raw is None:
        raise Unsupported(f"missing native image asset: {name}")
    return Image.open(BytesIO(raw))


def _inside(path):
    resolved = Path(path).resolve()
    if not any(resolved.is_relative_to(root) for root in _roots):
        raise Unsupported("image path outside plugin directories")
    return resolved


def _local_image(path):
    from sayu_compat import MAX_INPUT_IMAGE_BYTES

    with _inside(path).open("rb") as stream:
        return _decode(stream.read(MAX_INPUT_IMAGE_BYTES + 1))


def _response(url):
    from sayu_compat import MAX_INPUT_IMAGE_BYTES

    if _fetch is None:
        raise Unsupported("remote image requires a permitted HTTP transport")
    status, content = _fetch(url)
    if not isinstance(status, int) or not isinstance(content, bytes):
        raise TypeError("invalid image transport response")
    if len(content) > MAX_INPUT_IMAGE_BYTES:
        raise Unsupported("image bytes exceed the worker limit")
    return status, content


def crop_center_img(img, based_w, based_h):
    from sayu_compat import MAX_INPUT_PIXELS

    return _crop_center_img(img, based_w, based_h, MAX_INPUT_PIXELS, MAX_INPUT_PIXELS)


def _crop_center_img(img, based_w, based_h, canvas_limit, resize_limit):
    _pixels(img.size)
    if based_w * based_h > canvas_limit:
        raise Unsupported("image dimensions exceed the canvas limit")
    # Native compares two *formatted strings*, then calculates both sizes
    # with the rounded source ratio. Numeric aspect comparison differs at 10:1.
    target_ratio = f"{based_w / based_h:.3f}"
    source_ratio = f"{img.width / img.height:.3f}"
    width = math.ceil(based_h * float(source_ratio))
    height = math.ceil(based_w / float(source_ratio))
    size = (width, based_h) if source_ratio > target_ratio else (based_w, height)
    if size[0] * size[1] > resize_limit:
        raise Unsupported("intermediate resize exceeds the canvas limit")
    left = (size[0] - based_w) / 2
    top = (size[1] - based_h) / 2
    with img.resize(size, Image.Resampling.LANCZOS) as resized:
        return resized.crop(
            (int(left), int(top), int(left + based_w), int(top + based_h))
        )


def _background_color(image, is_light):
    with image.quantize(colors=8, method=Image.Quantize.FASTOCTREE) as quantized:
        palette = quantized.getpalette()
    colors = (tuple(palette[i : i + 3]) for i in range(0, 24, 3))
    target = 195 if is_light else 120
    return min(colors, key=lambda c: abs(c[0] * 0.3 + c[1] * 0.6 + c[2] * 0.1 - target))


def easy_alpha_composite(im, im_paste, pos=(0, 0), direction="lt"):
    _pixels(im.size)
    _pixels(im_paste.size)
    x, y = pos
    w, h = im_paste.size
    if "d" in direction:
        y -= h
    if "r" in direction:
        x -= w
    if "c" in direction:
        x -= w // 2
        y -= h // 2
    layer = Image.new("RGBA", im.size)
    layer.paste(im_paste, (x, y, x + w, y + h), im_paste)
    return Image.alpha_composite(im, layer)


def draw_text_by_line(
    img, pos, text, font, fill, max_length, center=False, line_space=None
):
    def measure(value):
        if hasattr(font, "getsize"):
            return font.getsize(value)
        left, top, right, bottom = font.getbbox(value)
        return right - left, bottom - top

    x, y = pos
    unit_width, height = measure("X")
    step = math.ceil(height * 1.3 if line_space is None else height + line_space)
    draw = ImageDraw.Draw(img)
    row = ""
    length = 0
    for char in text:
        width = measure(char)[0] if hasattr(font, "getsize") else unit_width
        row += char
        if length + width * 2 <= max_length:
            length += width
            continue
        if center:
            # On Pillow >= 10 the original centers a wrapped row by its last glyph.
            span = measure(row if hasattr(font, "getsize") else char)[0]
            x = math.ceil((img.width - span) / 2)
        draw.text((x, y), row, font=font, fill=fill)
        row, length = "", 0
        y += step
    if row:
        if center:
            x = math.ceil((img.width - measure(row)[0]) / 2)
        draw.text((x, y), row, font=font, fill=fill)
    return y


class CustomizeImage:
    def __init__(self, bg_path):
        self.bg_path = bg_path

    def get_image(self, image, based_w, based_h):
        if isinstance(image, Image.Image):
            source = image
        elif image:
            _, raw = _response(image)
            source = _decode(raw)
        else:
            choices = list(_inside(self.bg_path).iterdir())
            source = (
                _local_image(random.choice(choices))
                if choices
                else _asset("bg.jpg").convert("RGBA")
            )
        return crop_center_img(source, based_w, based_h)

    @staticmethod
    def get_dominant_color(pil_img):
        _pixels(pil_img.size)
        return (
            pil_img.convert("RGBA")
            .resize((1, 1), Image.Resampling.NEAREST)
            .getpixel((0, 0))
        )

    @staticmethod
    def get_bg_color(edit_bg, is_light=False):
        _pixels(edit_bg.size)
        return _background_color(edit_bg, is_light)

    @staticmethod
    def get_text_color(bg_color):
        delta = -125 if max(*bg_color) > 130 else 125
        return tuple(math.floor(min(value + delta, 255)) for value in bg_color[:3])

    @staticmethod
    def _detail(color, offset):
        delta = -140 if max(*color) > 115 else 140
        return tuple(
            math.floor(v + offset if v + delta <= 255 else 255) for v in color[:3]
        )

    @staticmethod
    def get_char_color(bg_color):
        return CustomizeImage._detail(bg_color, 5)

    @staticmethod
    def get_char_high_color(bg_color):
        return CustomizeImage._detail(bg_color, 20)

    @staticmethod
    def get_bg_detail_color(bg_color):
        return CustomizeImage._detail(bg_color, -20)

    @staticmethod
    def get_highlight_color(color):
        adjusted = [v - 127 if v > 127 else 127 for v in color[:3]]
        # Ties in the native red, green, blue iteration keep the last channel.
        channel = max(range(3), key=lambda i: (adjusted[i], i))
        adjusted[channel] = color[channel]
        return tuple(adjusted)


async def draw_pic_with_ring(pic, size, bg_color=None, is_ring=True):
    _pixels((size, size))
    result = Image.new("RGBA", (size, size))
    face = crop_center_img(pic, size, size).convert("RGBA")
    if bg_color:
        background = Image.new("RGBA", (size, size), bg_color)
        background.paste(face, (0, 0), face)
        face = background
    result.paste(face, (0, 0), _asset("mask.png").resize(result.size))
    if is_ring:
        ring = _asset("ring.png").resize(result.size)
        result.paste(ring, (0, 0), ring)
    return result


async def get_pic(url, size=None):
    status, content = await asyncio.to_thread(_response, url)
    if size is not None:
        _pixels(size)
    if status != 200:
        return Image.new("RGBA", (960, 600) if size is None else size)
    image = _decode(content)
    return image if size is None else image.resize(size)


async def _avatar(url):
    try:
        _, content = await asyncio.to_thread(_response, url)
        return _decode(content)
    except (OSError, TimeoutError):
        return None


async def get_event_avatar(ev, avatar_path=None):
    image = None
    if ev.bot_id == "onebot" and ev.at:
        image = await _avatar(f"http://q1.qlogo.cn/g?b=qq&nk={ev.at}&s=640")
    if image is None and "avatar" in ev.sender and ev.sender["avatar"]:
        url = ev.sender["avatar"]
        if url.startswith(("http", "https")):
            image = await _avatar(url)
    if image is None and ev.bot_id == "onebot" and not ev.sender:
        url = (
            f"http://q1.qlogo.cn/g?b=qq&nk={ev.user_id}&s=640"
            if ev.user_id
            else "https://q1.qlogo.cn/g?b=qq&nk=3399214199&s=640"
        )
        image = await _avatar(url)
    elif image is None and ev.bot_id == "qqgroup" and ev.bot_self_id and ev.user_id:
        image = await _avatar(
            f"https://q.qlogo.cn/qqapp/{ev.bot_self_id}/{ev.user_id}/100"
        )
    if image is None and avatar_path:
        choices = list(_inside(avatar_path).iterdir())
        if choices:
            image = _local_image(random.choice(choices))
    return _asset("icon.jpg").convert("RGBA") if image is None else image


async def get_avatar_with_ring(
    ev, size=300, avatar_path=None, bg_color=None, is_ring=True
):
    image = await get_event_avatar(ev, avatar_path)
    return await draw_pic_with_ring(image, size, bg_color, is_ring)


async def get_color_bg(
    based_w,
    based_h,
    bg_path=None,
    without_mask=False,
    is_full=False,
    color=None,
    full_opacity=200,
):
    if bg_path is None:
        from gsuid_core.data_store import get_res_path

        bg_path = get_res_path(["GsCore", "bg"])
    custom = CustomizeImage(bg_path)
    image = custom.get_image(None, based_w, based_h)
    if color is None:
        color = custom.get_bg_color(image)
    if is_full or not without_mask:
        size = (based_w, based_h)
        layer = Image.new("RGBA", size, color)
        mask = (
            Image.new("RGBA", size, (255, 255, 255, full_opacity))
            if is_full
            else _asset("bg_mask.png").resize(size)
        )
        image.paste(layer, (0, 0), mask)
    return image


def install_image_tools(package, source_root, data_root, *, fetch=None):
    """Install the native import name and bind this generation's local/remote reads."""
    global _roots, _fetch
    _roots = tuple(Path(p).resolve() for p in (source_root, data_root) if p is not None)
    _fetch = fetch
    module = package("gsuid_core.utils.image.image_tools")
    for helper in (
        crop_center_img,
        easy_alpha_composite,
        draw_text_by_line,
        CustomizeImage,
        draw_pic_with_ring,
        get_color_bg,
        get_event_avatar,
        get_avatar_with_ring,
        get_pic,
    ):
        setattr(module, helper.__name__, helper)
    return module
