"""Compare native image_tools with the packaged port, with controlled HTTP responses."""

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import tempfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build_local import worker_archive  # noqa: E402
from image_assets import verified_assets  # noqa: E402
from native_contract import REFERENCE_COMMIT, copy_reference, offline  # noqa: E402


def fingerprint(image):
    return [image.mode, image.size, hashlib.sha256(image.tobytes()).hexdigest()]


def failure(callback):
    try:
        callback()
    except Exception as exc:
        return type(exc).__name__
    raise AssertionError("expected error")


async def compare_cases(kind, work, archive, font_file):
    from PIL import Image, ImageFont, __version__

    sys.addaudithook(offline)
    result = {}
    sample = Image.new("RGBA", (17, 9))
    sample.putdata(
        [
            (19 * x % 256, 235 - 9 * y, x + y, 13 * x + 5 * y)
            for y in range(9)
            for x in range(17)
        ]
    )
    stream = BytesIO()
    sample.save(stream, format="PNG")
    raw = stream.getvalue()
    local = work / "photos"
    local.mkdir()
    (local / "sample.png").write_bytes(raw)
    empty = work / "empty"
    empty.mkdir()
    calls = []

    def fetch(url):
        calls.append(url)
        if url.endswith("/failure"):
            raise OSError("fixture transport failure")
        if url.endswith("/corrupt"):
            return 200, b"invalid image"
        if url.endswith("/absent"):
            return 404, b"missing"
        return 200, raw

    if kind == "native":
        sys.path.insert(0, str(work))
        from gsuid_core.utils.image import image_tools as port

        def response(url):
            code, data = fetch(url)
            return SimpleNamespace(status_code=code, content=data, read=lambda: data)

        class Client:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                pass

            async def get(self, url):
                return response(url)

        async def sget(url, **kwargs):
            return response(url)

        port.get = response
        port.sget = sget
        port.httpx2.AsyncClient = Client
    else:
        sys.path.insert(0, str(archive))
        from plugin_image_tools import install_image_tools
        from sayu_compat import install, package

        install("image_fixture", "", work, source_root=work)
        port = install_image_tools(package, work, work, fetch=fetch)
        from gsuid_core.utils.image import image_tools

        assert port is image_tools

    for mode in ("RGBA", "RGB", "L", "P"):
        img = sample.convert(mode)
        for size in ((7, 7), (4, 9), (12, 3), (50, 3), (3, 50)):
            result[f"crop/{mode}/{size}"] = fingerprint(
                port.crop_center_img(img, *size)
            )
    result["zero-height"] = failure(lambda: port.crop_center_img(sample, 4, 0))
    result["negative-width"] = failure(lambda: port.crop_center_img(sample, -1, 7))
    base = Image.new("RGBA", (37, 21), (50, 90, 70, 140))
    for direction in ("lt", "rd", "c", "cdr"):
        image = port.easy_alpha_composite(base, sample, (20, 9), direction)
        result["composite/" + direction] = fingerprint(image)
    result["unmodified-base"] = fingerprint(base)
    result["invalid-alpha"] = failure(
        lambda: port.easy_alpha_composite(base, sample.convert("RGB"))
    )
    font = ImageFont.truetype(str(font_file), 16)
    for center in (True, False):
        for text in ("", "Wit 一二三!", "Wit 一二三\n!"):
            for space in (None, 5):
                image = Image.new("RGB", (149, 85))
                try:
                    y = port.draw_text_by_line(
                        image, (3, 2), text, font, "white", 28, center, space
                    )
                    value = [y, fingerprint(image)]
                except Exception as exc:
                    value = type(exc).__name__
                result[f"text/{center}/{text}/{space}"] = value
    for bg_color, ring in ((None, False), (None, True), ((10, 20, 30), True)):
        result[f"ring/{bg_color}/{ring}"] = fingerprint(
            await port.draw_pic_with_ring(sample, 39, bg_color, ring)
        )
    background = port.CustomizeImage(local)
    for src in (None, sample, "https://fixture.invalid/picture"):
        result["background/" + type(src).__name__] = fingerprint(
            background.get_image(src, 31, 29)
        )
    for color in ((0, 0, 0), (130, 130, 130), (10, 147, 240), (255, 255, 255)):
        result[f"colors/{color}"] = [
            getattr(background, name)(color)
            for name in (
                "get_text_color",
                "get_char_color",
                "get_char_high_color",
                "get_bg_detail_color",
                "get_highlight_color",
            )
        ]
    result["dominant"] = background.get_dominant_color(sample)
    result["bg-light"] = background.get_bg_color(sample, True)
    result["bg-dark"] = background.get_bg_color(sample)
    for i, path in enumerate((local, empty, None)):
        for mask, full in ((False, False), (True, False), (False, True)):
            result[f"bg/{i}/{mask}/{full}"] = fingerprint(
                await port.get_color_bg(41, 23, path, mask, full)
            )
    result["missing-directory"] = failure(
        lambda: port.CustomizeImage(work / "missing").get_image(None, 7, 7)
    )
    event = SimpleNamespace(
        bot_id="astrbot", at="", user_id="u", bot_self_id="b", sender={}
    )
    for idx, (bot, at, sender, path) in enumerate(
        (
            ("astrbot", "", {}, local),
            ("astrbot", "", {}, empty),
            ("onebot", "test", {}, None),
            ("onebot", "", {}, None),
            ("qqgroup", "", {}, None),
            ("astrbot", "", {"avatar": "https://fixture.invalid/failure"}, local),
            ("astrbot", "", {"avatar": "https://fixture.invalid/corrupt"}, None),
            ("astrbot", "", {"avatar": "https://fixture.invalid/avatar"}, None),
        )
    ):
        event.bot_id, event.at, event.sender = bot, at, sender
        result[f"avatar/{idx}"] = fingerprint(await port.get_event_avatar(event, path))
    result["avatar-ring"] = fingerprint(
        await port.get_avatar_with_ring(event, 45, bg_color=(50, 80, 20))
    )
    for name in ("picture", "absent", "failure", "corrupt"):
        for size in (None, (19, 17)):
            try:
                value = fingerprint(
                    await port.get_pic("https://fixture.invalid/" + name, size)
                )
            except Exception as exc:
                value = type(exc).__name__
            result[f"http/{name}/{size}"] = value
    result["HTTP calls"] = calls
    gaps = {}
    if kind == "loader":
        port = install_image_tools(package, work, work)
        from port_errors import Unsupported

        for name, fn in (
            (
                "no_transport",
                lambda: port.CustomizeImage(local).get_image(
                    "https://fixture.invalid/denied", 2, 2
                ),
            ),
            (
                "external_path",
                lambda: port.CustomizeImage(work.parent).get_image(None, 9, 9),
            ),
            ("output_limit", lambda: port.crop_center_img(sample, 5000, 5000)),
            (
                "intermediate_limit",
                lambda: port.crop_center_img(Image.new("RGB", (9, 1)), 1, 2200),
            ),
        ):
            gaps[name] = failure(fn)
            assert gaps[name] == Unsupported.__name__, (name, gaps[name])
    return {"pillow": __version__, "observations": result, "loader_bounds": gaps}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reference", type=Path, default=ROOT / ".sayu-local/reference"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--loader-python", type=Path, default=Path(sys.executable))
    parser.add_argument("--child", choices=("native", "loader"))
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--font", type=Path)
    args = parser.parse_args()
    if args.child:
        report = asyncio.run(
            compare_cases(args.child, Path.cwd(), args.archive, args.font)
        )
    else:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            native, loader = root / "native", root / "loader"
            native.mkdir()
            loader.mkdir()
            sources = copy_reference(args.reference.resolve(), native)
            archive = root / "worker.pyz"
            archive.write_bytes(
                worker_archive(
                    json.loads((ROOT / "profiles/lock.json").read_bytes())["waves"]
                )
            )
            with ZipFile(archive) as built:
                for name, data in verified_assets(args.reference).items():
                    assert built.read(name) == data
                assert built.read("licenses/gsuid_core-COPYING.txt")
            font = native / "gsuid_core/utils/fonts/MiSansVF.ttf"
            runs = {}
            for kind, interpreter in (
                ("native", sys.executable),
                ("loader", args.loader_python.resolve()),
            ):
                output = root / (kind + ".json")
                subprocess.run(
                    [
                        str(interpreter),
                        "-IB",
                        "-X",
                        "utf8",
                        __file__,
                        "--child",
                        kind,
                        "--output",
                        str(output),
                        "--archive",
                        str(archive),
                        "--font",
                        str(font),
                    ],
                    cwd=root / kind,
                    check=True,
                    timeout=90,
                )
                runs[kind] = json.loads(output.read_bytes())
            expected, observed = [runs[k]["observations"] for k in ("native", "loader")]
            report = {
                "reference": REFERENCE_COMMIT,
                "sources": sources,
                "worker_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                **runs,
                "differences": [
                    key
                    for key in expected.keys() | observed.keys()
                    if expected.get(key) != observed.get(key)
                ],
            }
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if report.get("differences"):
        raise SystemExit(f"native differences: {report['differences']}")


if __name__ == "__main__":
    main()
