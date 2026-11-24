"""Build-only input for gsuid_core image_tools, pinned to native commit and bytes."""

from hashlib import sha256
from pathlib import Path
from urllib.request import urlopen

COMMIT = "96cd7353be3100ff982bccc33e554961837c778e"
BASE = f"https://raw.githubusercontent.com/Genshin-bots/gsuid_core/{COMMIT}/"
TEXTURES = "gsuid_core/utils/image/texture2d/"
ASSETS = {
    TEXTURES + "div.png": "82d81f2c96c538f72316179d5f1573214add4055e3924661612958a7b7ed89f9",
    TEXTURES + "error_img.png": "cb8eca2fcb2c84b966886f0335d3b279dea6b08798ad95335683920860ab2f89",
    "gsuid_core/utils/default_bg/bg.jpg": "2ed64b8ff94ccd5f2ce159cf2ae1cdde2621892ddf0895f29149398aec2558fa",
    TEXTURES
    + "bg_mask.png": "ce3cc2d647a28fa8aa321dbbc196891f5daabb47deb77beb8f1394c8254bd286",
    TEXTURES
    + "icon.jpg": "6adf4b87363e162338bf27421bf037809386e957fbf0e5a9de9d73f0c50ef8f2",
    TEXTURES
    + "mask.png": "3af7d6148c94ad30f60ce0dd857bdcb36a2cad12f25c08bebb54e94b42445921",
    TEXTURES
    + "ring.png": "0d0b96c127b4feb202bd5eaf1f0629515fe454abde266da3fc99edcb20e04032",
}


def verified_assets(reference=None):
    """Return worker archive members. A provided local tree takes precedence."""
    members = {}
    for name, expected in ASSETS.items():
        if reference is not None and Path(reference).is_dir():
            stream = (Path(reference) / name).open("rb")
        else:
            stream = urlopen(BASE + name, timeout=30)
        with stream:
            data = stream.read(256 * 1024 + 1)
        if sha256(data).hexdigest() != expected:
            raise ValueError(f"native image asset checksum mismatch: {name}")
        members["plugin_image_assets/" + Path(name).name] = data
    return members
