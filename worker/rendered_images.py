"""Pixel and allocation budgets for the worker's fixed local drawing functions."""

from collections import OrderedDict
from functools import wraps
from hashlib import sha256
from io import BytesIO
from threading import Lock

from PIL import Image

from port_errors import Unsupported

# Pillow allocates four bytes per RGB/RGBA canvas pixel. The reference render's
# 2970 x 5584 output and 3597 x 5584 resize raised peak working set by 197 MiB.
# Serialize that operation and cap each full-sized surface before allocating it.
CANVAS_PIXELS = 64 * 1024**2 // 4
RESIZE_PIXELS = 80 * 1024**2 // 4
_render_lock = Lock()
_known_lock = Lock()
_known = OrderedDict()


def serialized_render(fn):
    @wraps(fn)
    def render(*args, **kwargs):
        with _render_lock:
            return fn(*args, **kwargs)

    return render


def clear_generated():
    with _known_lock:
        _known.clear()


def _remember(data, size):
    from sayu_compat import MAX_INPUT_PIXELS

    if size[0] * size[1] > MAX_INPUT_PIXELS:
        key = sha256(data).digest()
        with _known_lock:
            _known[key] = size
            _known.move_to_end(key)
            if len(_known) > 128:
                _known.popitem(last=False)


def generated(data, size):
    key = sha256(data).digest()
    with _known_lock:
        return _known.get(key) == size


def _check(size, limit):
    w, h = size
    if min(w, h) < 1 or max(w, h) > 16384 or w * h > limit:
        raise Unsupported("local drawing exceeds its surface budget")


def crop_canvas(image, w, h):
    from plugin_image_tools import _crop_center_img

    _check((w, h), CANVAS_PIXELS)
    return _crop_center_img(image, w, h, CANVAS_PIXELS, RESIZE_PIXELS)


def canvas_color(image, is_light):
    from plugin_image_tools import _background_color

    _check(image.size, CANVAS_PIXELS)
    return _background_color(image, is_light)


class _EncodedCanvas(BytesIO):
    def write(self, data):
        from sayu_compat import MAX_INPUT_IMAGE_BYTES

        if self.tell() + len(data) > MAX_INPUT_IMAGE_BYTES:
            raise Unsupported("encoded canvas exceeds the worker byte limit")
        return super().write(data)


def resize_decoration(image, size):
    # The renderer scales banners and category strips before creating its canvas.
    from sayu_compat import MAX_INPUT_PIXELS

    _check(size, MAX_INPUT_PIXELS)
    return image.resize(size)


def encode_canvas(image):
    from sayu_compat import _media_bytes

    _check(image.size, CANVAS_PIXELS)
    data = _EncodedCanvas()
    if image.mode == "RGB":
        image.save(data, "JPEG", quality=85)
    else:
        with image.convert("RGB") as rgb:
            rgb.save(data, "JPEG", quality=85)
    out = _media_bytes(data.getvalue())
    _remember(out, image.size)
    return out


def save_canvas(image, path, format, **options):
    from plugin_downloads import Downloads
    from plugin_help import _safe_image
    from sayu_compat import _media_bytes

    _check(image.size, CANVAS_PIXELS)
    output = _EncodedCanvas()
    image.save(output, format, **options)
    raw = _media_bytes(output.getvalue())
    Downloads._write(_safe_image(path), raw)
    _remember(raw, image.size)


def open_cached_canvas(path):
    from plugin_help import _safe_image
    from sayu_compat import MAX_INPUT_IMAGE_BYTES, MAX_INPUT_PIXELS

    with _safe_image(path).open("rb") as stream:
        data = stream.read(MAX_INPUT_IMAGE_BYTES + 1)
    if len(data) > MAX_INPUT_IMAGE_BYTES:
        raise Unsupported("help cache exceeds the worker byte limit")
    image = Image.open(BytesIO(data), formats=("JPEG",))
    if image.width * image.height > MAX_INPUT_PIXELS and not generated(
        data, image.size
    ):
        image.close()
        raise Unsupported("unrecognized oversized help cache")
    return image
