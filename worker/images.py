"""Fit bounded static images to public card assets or the owner-scoped PNG transport."""

import base64
import binascii
from io import BytesIO
from math import sqrt

from PIL import Image, ImageDraw

from sayu_compat import (
    MAX_INPUT_IMAGE_BYTES,
    MAX_INPUT_PIXELS,
    UnsupportedMessage,
    worker_font,
)


MAX_WIDTH = 2048
MAX_HEIGHT = 8192
MAX_PIXELS = (128 * 1024 * 1024) // 36
MAX_BYTES = 4 * 1024 * 1024
PUBLIC_BYTES = 1024 * 1024
PUBLIC_SIDE = 4096
PUBLIC_PIXELS = 8_000_000
# Below half-size, even ordinary 24 px diagram labels fall below 12 px.
MIN_SCALE = 0.5
PADDING = 16


def _fit_scale(width, height, max_width, max_height, max_pixels):
    scale = min(
        1.0, max_width / width, max_height / height, sqrt(max_pixels / (width * height))
    )
    if scale < MIN_SCALE:
        raise UnsupportedMessage("image would be too small to read")
    return scale


def _scaled_size(size, scale):
    return tuple(max(1, int(length * scale)) for length in size)


def _decode(encoded, remaining_pixels=MAX_INPUT_PIXELS, *, single_budget=None):
    if not isinstance(encoded, str) or len(encoded) > 4 * (
        (MAX_INPUT_IMAGE_BYTES + 2) // 3
    ):
        raise UnsupportedMessage("invalid encoded image size")
    try:
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > MAX_INPUT_IMAGE_BYTES:
            raise UnsupportedMessage("image exceeds source byte limit")
        with Image.open(BytesIO(raw), formats=("JPEG", "PNG")) as image:
            if image.width * image.height > remaining_pixels:
                from rendered_images import generated

                if single_budget is None or not generated(raw, image.size):
                    raise UnsupportedMessage("image exceeds source pixel limit")
                # Check recipient readability against the original dimensions
                # before allocating or resampling an authenticated local canvas.
                _fit_scale(*image.size, *single_budget)
            if getattr(image, "is_animated", False):
                raise UnsupportedMessage("animated images cannot be projected")
            has_alpha = "A" in image.getbands() or "transparency" in image.info
            clean = image.convert("RGBA" if has_alpha else "RGB")
            # The scoped transport validates a PNG containing just IHDR, IDAT and IEND.
            clean.info.clear()
            return clean, len(raw)
    except UnsupportedMessage:
        raise
    except (ValueError, binascii.Error, OSError, Image.DecompressionBombError) as exc:
        raise UnsupportedMessage("invalid image for private transport") from exc


def _encode(image, scale, budget, *, public=False, minimum=MIN_SCALE):
    # Always resample from the original pixels, including the final minimum-size attempt.
    while True:
        size = _scaled_size(image.size, scale)
        frame = (
            image
            if size == image.size
            else image.resize(size, Image.Resampling.LANCZOS)
        )
        stream = BytesIO()
        frame.save(stream, format="PNG")
        if public and stream.tell() > budget and frame.mode == "RGB":
            stream = BytesIO()
            frame.save(stream, format="JPEG", quality=90, subsampling=0)
        if public and stream.tell() > budget and frame.mode == "RGBA":
            # Card PNGs support indexed transparency. Retain the alpha channel
            # and try a compact palette before sacrificing label resolution.
            stream = BytesIO()
            frame.quantize(colors=256, method=Image.Quantize.FASTOCTREE).save(
                stream, format="PNG", optimize=True
            )
        if stream.tell() <= budget:
            return {"image": base64.b64encode(stream.getvalue()).decode("ascii")}
        if scale <= minimum:
            raise UnsupportedMessage("image exceeds byte limit at readable resolution")
        scale = max(minimum, scale * min(0.9, sqrt(budget / stream.tell())))


def prepare_public_parts(parts, source, package):
    if sum("image" in part for part in parts) > 1:
        # The public command card has one 640 px image slot for this
        # projection. Check readability at the size the recipient sees.
        return _compose(parts, source, package, public=True)
    result = []
    remaining_pixels = MAX_INPUT_PIXELS
    for part in parts:
        if part.keys() == {"text"}:
            result.append(part)
            continue
        if part.keys() != {"image"}:
            raise UnsupportedMessage("unknown public message part")
        image, byte_count = _decode(
            part["image"],
            remaining_pixels,
            single_budget=(PUBLIC_SIDE, PUBLIC_SIDE, PUBLIC_PIXELS),
        )
        remaining_pixels -= image.width * image.height
        scale = _fit_scale(*image.size, PUBLIC_SIDE, PUBLIC_SIDE, PUBLIC_PIXELS)
        if scale == 1.0 and byte_count <= PUBLIC_BYTES:
            result.append(part)
        else:
            result.append(_encode(image, scale, PUBLIC_BYTES, public=True))
    if sum(len(part.get("image", "")) for part in result) > 4 * ((MAX_BYTES + 2) // 3):
        raise UnsupportedMessage("image chain exceeds worker transport")
    return result


def _wrap_text(text, font, width):
    """Split at rendered widths and preserve explicit lines and whitespace."""
    lines = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        while line and font.getlength(line) > width:
            lo, hi = 0, len(line)
            while hi - lo > 1:
                mid = (hi + lo) // 2
                if font.getlength(line[:mid]) <= width:
                    lo = mid
                else:
                    hi = mid
            if not lo:
                raise UnsupportedMessage("text exceeds image width")
            lines.append(line[:lo])
            line = line[lo:]
        lines.append(line)
    return lines


def prepare_owner_parts(parts, source, package):
    """Return text unchanged or a single PNG containing the entire reply in order."""
    return _compose(parts, source, package, public=False)


def _compose(parts, source, package, *, public):
    if not parts or len(parts) > 32:
        raise UnsupportedMessage("invalid message chain")
    if all(
        isinstance(part, dict)
        and part.keys() == {"text"}
        and isinstance(part["text"], str)
        for part in parts
    ):
        return parts

    content = []
    width = height = pixel_count = 0
    has_text = False
    total_text = 0
    for part in parts:
        if isinstance(part, dict) and part.keys() == {"image"}:
            image, _ = _decode(
                part["image"],
                MAX_INPUT_PIXELS - pixel_count,
                single_budget=(MAX_WIDTH, MAX_HEIGHT, MAX_PIXELS)
                if len(parts) == 1
                else None,
            )
            pixel_count += image.width * image.height
            width = max(width, image.width)
            content.append(image)
        elif (
            isinstance(part, dict)
            and part.keys() == {"text"}
            and isinstance(part["text"], str)
        ):
            text = part["text"]
            total_text += len(text)
            if total_text > 4096:
                raise UnsupportedMessage("text exceeds reply limit")
            has_text = True
            content.append(text)
        else:
            raise UnsupportedMessage("unknown message part")

    if len(content) == 1:
        image = content[0]
        scale = _fit_scale(*image.size, MAX_WIDTH, MAX_HEIGHT, MAX_PIXELS)
        return [_encode(image, scale, MAX_BYTES)]

    # Preserve image pixels at native resolution. Lay the text out against the same width.
    font = None
    if has_text:
        width = max(width, 640)
        font = worker_font(source, package, 24)
        ascent, descent = font.getmetrics()
        line_height = ascent + descent + 6
    layout = []
    for item in content:
        if isinstance(item, str):
            lines = _wrap_text(item, font, width - 2 * PADDING)
            layout.append((height + PADDING, lines))
            height += 2 * PADDING + len(lines) * line_height
        else:
            layout.append((height, item))
            height += item.height
    if public:
        scale = _fit_scale(width, height, 640, 640, PUBLIC_PIXELS)
    else:
        scale = _fit_scale(width, height, MAX_WIDTH, MAX_HEIGHT, MAX_PIXELS)

    alpha = any(
        isinstance(item, Image.Image) and item.mode == "RGBA" for item in content
    )
    canvas = Image.new(
        "RGBA" if alpha else "RGB", _scaled_size((width, height), scale), "white"
    )
    draw = ImageDraw.Draw(canvas)
    if has_text and scale < 1:
        font = worker_font(source, package, int(24 * scale))
    for y, item in layout:
        if isinstance(item, list):
            for line in item:
                draw.text(
                    (int(PADDING * scale), int(y * scale)),
                    line,
                    font=font,
                    fill="black",
                    anchor="la",
                )
                y += line_height
        else:
            if scale < 1:
                if int((y + item.height) * scale) <= int(y * scale):
                    raise UnsupportedMessage("scaled message would omit an image")
                item = item.resize(
                    _scaled_size(item.size, scale), Image.Resampling.LANCZOS
                )
            canvas.paste(item, ((canvas.width - item.width) // 2, int(y * scale)))
    return [
        _encode(
            canvas,
            1.0,
            PUBLIC_BYTES if public else MAX_BYTES,
            public=public,
            minimum=MIN_SCALE / scale,
        )
    ]
