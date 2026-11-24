"""Build the two standalone Pillow renderers from verified GsCore help sources."""

import ast
import hashlib
import io
import json
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen
import zipfile

from image_assets import BASE
from snapshot import zip_entry

HELP = "gsuid_core/help/"
MODERN = HELP + "draw_new_plugin_help.py"
CLASSIC = HELP + "draw_plugin_help.py"
IMAGE_TOOLS = "gsuid_core/utils/image/image_tools.py"
FONT = "gsuid_core/utils/fonts/MiSansVF.ttf"
HASHES = {
    MODERN: "c1a2f1986710f061485bb427a288a4106cd7265df8146936467bdb9582d0e92b",
    CLASSIC: "60eb70ddf45141408e1f05a26fd849c9b962e6878ad9141c4c97b77e07e346ee",
    FONT: "0ddef90648998900175cfdca9a6f087a2544c182f130b0ad4f7e94a03a115e79",
    "ICON.png": "f23a4ece770d0af22d7493ba04e0fd40d7a8ddb848596a4944c54e7094d6f871",
    IMAGE_TOOLS: "482b90c61ba786af5b31adf4e4de6f361aeebd88ab04db6ab3c90d53af90812b",
}
# Git tree IDs from gsuid_core at the same commit as the renderer source.
TREES = {
    "icon": "6a9ee2be1337b80badbbfefd50436a6d32340423",
    "new_icon": "a2ce225240e07cb7d04798e197ff01006a0a13dd",
    "texture2d": "8cd6011e03f15a16b85335e7007ff67eac0fb828",
}
NOTICE = """# Drawn from Genshin-bots/gsuid_core 96cd7353be3100ff982bccc33e554961837c778e.
# GPL-3.0, see licenses/gsuid_core-COPYING.txt.
"""


def _blob(reference, name, limit):
    if reference is not None and Path(reference).is_dir():
        stream = (Path(reference) / name).open("rb")
    else:
        stream = urlopen(BASE + quote(name), timeout=30)
    with stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("oversized help asset")
    return data


def _tree_files(reference, kind, tree):
    directory = HELP + kind + "/"
    if reference is not None and Path(reference).is_dir():
        names = [p.name for p in (Path(reference) / directory).iterdir()]
    else:
        url = "https://api.github.com/repos/Genshin-bots/gsuid_core/git/trees/" + tree
        with urlopen(url, timeout=30) as response:
            names = [f["path"] for f in json.loads(response.read(100_000))["tree"]]
    listing = bytearray()
    files = {}
    for name in sorted(names, key=str.encode):
        if Path(name).name != name or Path(name).suffix not in {".png", ".jpg"}:
            raise ValueError("unexpected help asset")
        data = _blob(reference, directory + name, 2_000_000)
        digest = hashlib.sha1(f"blob {len(data)}\0".encode() + data).digest()
        listing.extend(b"100644 " + name.encode() + b"\0" + digest)
        files[kind + "/" + name] = data
    if hashlib.sha1(f"tree {len(listing)}\0".encode() + listing).hexdigest() != tree:
        raise ValueError("native help texture tree checksum mismatch")
    return files


def verified_help(reference=None):
    inputs = {}
    for name, expected in HASHES.items():
        data = _blob(reference, name, 21_000_000 if name == FONT else 200_000)
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f"native help input checksum mismatch: {name}")
        inputs[name] = data

    # Preserve the original rendering bodies, including cache semantics,
    # permission filters, defaults and Pillow draw order. Only their host
    # dependencies and the location of immutable images change.
    result = {"plugin_help_native/__init__.py": b""}
    for filename in (MODERN, CLASSIC):
        src = inputs[filename].decode()
        src = src.replace("from .model import", "from gsuid_core.help.model import")
        src = src.replace(
            "from gsuid_core.utils.plugins_config.gs_config import pic_gen_config",
            "from plugin_help import asset_root, open_help_image",
        )
        src = src.replace('pic_gen_config.get_config("PicQuality").data', "85")
        src = src.replace("Path(__file__).parent", "asset_root()")
        if filename == MODERN:
            # Only the fixed renderer can issue canvas outputs; untrusted asset
            # inputs and all other SDK callers retain the ordinary image limit.
            src = src.replace(
                "from gsuid_core.utils.image.convert import convert_img_sync",
                "from rendered_images import (crop_canvas as crop_center_img, "
                "encode_canvas as convert_img_sync, serialized_render, open_cached_canvas, "
                "save_canvas, resize_decoration, canvas_color)",
            ).replace("    crop_center_img,\n", "")
            src = src.replace("CustomizeImage.get_bg_color(", "canvas_color(")
            src = src.replace(
                "@to_thread\ndef _get_new_help(",
                "@to_thread\n@serialized_render\ndef _get_new_help(",
            )
            src = src.replace("Image.open(help_path)", "open_cached_canvas(help_path)")
            src = src.replace(
                "img.save(\n        help_path,", "save_canvas(img,\n        help_path,"
            )
            for surface in ("banner_bg", "cag_bar"):
                src = src.replace(
                    f"{surface}.resize((w,", f"resize_decoration({surface}, (w,"
                )
        src = src.replace("Image.open(", "open_help_image(")
        result["plugin_help_native/" + Path(filename).name] = (NOTICE + src).encode()
    src = inputs[IMAGE_TOOLS].decode()
    helpers = [
        ast.get_source_segment(src, fn)
        for fn in ast.parse(src).body
        if isinstance(fn, ast.FunctionDef)
        and fn.name in {"tint_image", "draw_color_badge"}
    ]
    prelude = "from typing import Tuple, Union\nfrom PIL import Image, ImageDraw, ImageFont\nfrom plugin_help import core_font\n"
    result["plugin_help_native/image_helpers.py"] = (
        NOTICE + prelude + "\n\n".join(helpers) + "\n"
    ).encode()
    files = {"MiSansVF.ttf": inputs[FONT], "ICON.png": inputs["ICON.png"]}
    for kind, tree in TREES.items():
        files.update(_tree_files(reference, kind, tree))
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for path, data in sorted(files.items()):
            archive.writestr(zip_entry(path), data)
    result["plugin_help_assets.zip"] = stream.getvalue()
    return result
