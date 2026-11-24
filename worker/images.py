"""Fit bounded static images to public cards and owner-scoped image transports."""

import base64
import binascii
import re
from io import BytesIO
from math import ceil, sqrt

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


def _single_scan_jpeg(raw):
    """Accept a baseline, single interleaved scan that libjpeg decodes sequentially."""
    # A progressive or multi-scan decoder can retain full-resolution coefficient
    # planes even when draft() reduces its output. Only admit the bounded path.
    offset = 2
    components = None
    for _ in range(1024):
        if offset + 4 > len(raw) or raw[offset] != 0xFF:
            return False
        marker = raw[offset + 1]
        length = int.from_bytes(raw[offset + 2 : offset + 4], "big")
        end = offset + 2 + length
        if length < 2 or end > len(raw):
            return False
        if marker == 0xC0:
            if components is not None or length < 11 or raw[offset + 4] != 8:
                return False
            components = raw[offset + 9]
            if components not in (1, 3) or length != 8 + 3 * components:
                return False
        elif marker == 0xDA:
            if components is None or length != 6 + 2 * components:
                return False
            if raw[offset + 4] != components or raw[end - 3 : end] != b"\x00\x3f\x00":
                return False
            marker = _SCAN_END.search(raw, end)
            return (
                marker is not None
                and marker.start() == len(raw) - 2
                and raw[-1] == 0xD9
            )
        elif marker not in (0xC4, 0xDB, 0xDD, 0xFE) and not 0xE0 <= marker <= 0xEF:
            return False
        offset = end
    return False


_SCAN_END = re.compile(rb"\xff[^\x00\xd0-\xd7\xff]")


def _draft_poster(image, raw, remaining_pixels):
    width, height = image.size
    half = ((width + 1) // 2, (height + 1) // 2)
    if (
        image.format != "JPEG"
        or image.mode not in ("L", "RGB")
        or height <= max(1024, width)
        or half[0] > 1536
        or half[1] > MAX_HEIGHT
        or half[0] * half[1] > min(remaining_pixels, PUBLIC_PIXELS)
        or not _single_scan_jpeg(raw)
    ):
        raise UnsupportedMessage("image exceeds source pixel limit")
    # draft must precede every load/convert/resize. Never allocate the declared
    # full raster; check the decoder's actual destination before it runs.
    image.draft(image.mode, (max(1, width // 2), max(1, height // 2)))
    if image.size != half:
        raise UnsupportedMessage("decoder did not select readable half resolution")


def _decode(
    encoded,
    remaining_pixels=MAX_INPUT_PIXELS,
    *,
    single_budget=None,
    allow_reduced=False,
):
    if not isinstance(encoded, str) or len(encoded) > 4 * (
        (MAX_INPUT_IMAGE_BYTES + 2) // 3
    ):
        raise UnsupportedMessage("invalid encoded image size")
    try:
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > MAX_INPUT_IMAGE_BYTES:
            raise UnsupportedMessage("image exceeds source byte limit")
        with Image.open(BytesIO(raw), formats=("JPEG", "PNG")) as image:
            reduced = False
            if image.width * image.height > remaining_pixels:
                from rendered_images import generated

                if single_budget is not None and generated(raw, image.size):
                    _fit_scale(*image.size, *single_budget)
                elif allow_reduced:
                    _draft_poster(image, raw, remaining_pixels)
                    reduced = True
                else:
                    raise UnsupportedMessage("image exceeds source pixel limit")
            if getattr(image, "is_animated", False):
                raise UnsupportedMessage("animated images cannot be projected")
            if reduced:
                # A draft JPEG already has a canonical L or RGB raster. Decode in
                # place; convert() would keep a second full output buffer alive.
                image.load()
                image.info.clear()
                return image, len(raw), True
            has_alpha = "A" in image.getbands() or "transparency" in image.info
            clean = image.convert("RGBA" if has_alpha else "RGB")
            # The scoped transport validates a PNG containing just IHDR, IDAT and IEND.
            clean.info.clear()
            return clean, len(raw), False
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
        image, byte_count, _ = _decode(
            part["image"],
            remaining_pixels,
            single_budget=(PUBLIC_SIDE, PUBLIC_SIDE, PUBLIC_PIXELS),
        )
        remaining_pixels -= image.width * image.height
        if len(parts) == 1 and image.height > max(1024, image.width):
            return [_poster(image)]
        scale = _fit_scale(*image.size, PUBLIC_SIDE, PUBLIC_SIDE, PUBLIC_PIXELS)
        if scale == 1.0 and byte_count <= PUBLIC_BYTES:
            result.append(part)
        else:
            result.append(_encode(image, scale, PUBLIC_BYTES, public=True))
    if sum(len(part.get("image", "")) for part in result) > 4 * ((MAX_BYTES + 2) // 3):
        raise UnsupportedMessage("image chain exceeds worker transport")
    return result


def _poster(image):
    # The paginated renderer packs equal spans in row order. A tile
    # narrower than 108 px retains the complete image without a label overlay.
    # Resize the whole source once so adjacent tile boundaries share its pixels.
    scale = _fit_scale(*image.size, 1536, MAX_HEIGHT, PUBLIC_PIXELS)
    size = _scaled_size(image.size, scale)
    frame = (
        image if size == image.size else image.resize(size, Image.Resampling.LANCZOS)
    )
    width = max(400, frame.width)
    columns = max(8, ceil(width / 96))
    cell_width = ceil(width / columns)
    cell_height = 128
    rows = ceil(frame.height / cell_height)
    if columns * rows > 1000 or rows > 64:
        raise UnsupportedMessage("image exceeds the page limit")
    tiles = []
    total = 0
    for row in range(rows):
        for column in range(columns):
            x, y = column * cell_width, row * cell_height
            if x + cell_width <= frame.width and y + cell_height <= frame.height:
                tile = frame.crop((x, y, x + cell_width, y + cell_height))
            else:
                tile = Image.new("RGBA", (cell_width, cell_height))
                if x < frame.width:
                    tile.paste(
                        frame.crop(
                            (
                                x,
                                y,
                                min(x + cell_width, frame.width),
                                min(y + cell_height, frame.height),
                            )
                        ),
                        (0, 0),
                    )
            stream = BytesIO()
            tile.save(stream, format="PNG")
            data = stream.getvalue()
            total += len(data)
            if total > MAX_BYTES:
                raise UnsupportedMessage(
                    "lossless image tiles exceed the worker transport"
                )
            tiles.append(base64.b64encode(data).decode("ascii"))
    return {"poster": {"width": frame.width, "height": frame.height, "tiles": tiles}}


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
    """Preserve owner message order in text, a scoped PNG, or a paginated card."""
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
    reduced_source = False
    one_image = (
        sum(isinstance(part, dict) and part.keys() == {"image"} for part in parts) == 1
    )
    width = height = pixel_count = 0
    has_text = False
    total_text = 0
    for part in parts:
        if isinstance(part, dict) and part.keys() == {"image"}:
            image, _, reduced = _decode(
                part["image"],
                MAX_INPUT_PIXELS - pixel_count,
                allow_reduced=one_image and not public,
                single_budget=(MAX_WIDTH, MAX_HEIGHT, MAX_PIXELS)
                if len(parts) == 1
                else None,
            )
            reduced_source |= reduced
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
        if reduced_source:
            return [_poster(image)]
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
    if reduced_source:
        # draft already consumed the admissible reduction. Preserve this raster
        # and every caption in order, accounting for both source and destination
        # before allocating the composed canvas.
        if (
            width > 1536
            or height > MAX_HEIGHT
            or width * height > PUBLIC_PIXELS
            or pixel_count + width * height > MAX_INPUT_PIXELS
        ):
            raise UnsupportedMessage(
                "captioned poster exceeds the composite pixel budget"
            )
        scale = 1.0
    elif public:
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
    if reduced_source:
        return [_poster(canvas)]
    return [
        _encode(
            canvas,
            1.0,
            PUBLIC_BYTES if public else MAX_BYTES,
            public=public,
            minimum=MIN_SCALE / scale,
        )
    ]
