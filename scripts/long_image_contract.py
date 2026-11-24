"""Offline long-image contract through the packaged worker, public SDK card and WebChat sender."""

import argparse
import asyncio
import base64
import hashlib
import importlib
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
from types import ModuleType
from unittest.mock import patch
from zipfile import ZipFile
from io import BytesIO

from PIL import Image, ImageChops, ImageDraw, ImageStat

ROOT = Path(__file__).resolve().parents[1]


def digest(data):
    return hashlib.sha256(data).hexdigest()


async def worker_stage(work):
    sys.path.insert(0, str(work / "worker.pyz"))
    import game_profile
    import images
    import sayu_compat
    from worker_storage import install_offline_guard, install_write_guard

    namespace = {"__name__": "contract_worker"}
    with ZipFile(work / "worker.pyz") as archive:
        exec(
            compile(archive.read("__main__.py"), "worker.pyz/__main__.py", "exec"),
            namespace,
        )
    install_offline_guard()
    install_write_guard(work, work / "data")
    fixture = Image.new("RGB", (768, 2519))
    painter = ImageDraw.Draw(fixture)
    for y in range(fixture.height):
        painter.line(
            (0, y, fixture.width - 1, y),
            fill=(y % 251, (y // 251) * 23, (y * 19) % 251),
        )
    for y in range(4, fixture.height - 18, 41):
        painter.text((40, y), str(y), fill="white")
    fixture.save(work / "rows.png")
    encoded = base64.b64encode((work / "rows.png").read_bytes()).decode()
    result = images.prepare_public_parts(
        [{"image": encoded}], work, game_profile.PACKAGE
    )
    (work / "rows.json").write_text(json.dumps(result), encoding="utf8")

    worker = namespace["Worker"](work / "plugins", work / "data")
    await worker.start()
    original = sayu_compat.Bot.send

    async def capture(bot, message, *args, **kwargs):
        if isinstance(message, bytes):
            (work / "help.jpg").write_bytes(message)
        return await original(bot, message, *args, **kwargs)

    sayu_compat.Bot.send = capture
    try:
        random.seed(41)
        reply = await worker.handle(
            {
                "text": "gs帮助",
                "event": {
                    "actor_id": "fixture",
                    "adapter_id": "astrbot",
                    "conversation_kind": "direct",
                    "group_id": "",
                    "message_id": "fixture",
                },
                "private_image": False,
            }
        )
        (work / "help.json").write_text(json.dumps(reply["parts"]), encoding="utf8")
    finally:
        await worker.plugin.stop()


async def render_case(core, work, name):
    def load(module):
        return importlib.import_module(core.name + "." + module)

    sdk = load("yomihime_game_link_sdk")
    sys.modules["sayu_loader._api_v2"] = sdk
    runtime = ModuleType("sayu_loader.sayu.runtime")
    runtime.WORKERS = {}
    sys.modules[runtime.__name__] = runtime
    adapter = importlib.import_module("sayu_loader.sayu.module")
    handler = adapter.Module("genshin", None).handlers().capabilities["query"]
    parts = json.loads((work / (name + ".json")).read_bytes())
    source_file = work / ("rows.png" if name == "rows" else "help.jpg")
    source = Image.open(source_file).convert("RGB")
    poster = parts[0]["poster"]
    expected = source.resize(
        (poster["width"], poster["height"]), Image.Resampling.LANCZOS
    )
    title = "gs帮助" if name == "help" else "行序图"
    doc = await handler.document(None, title, parts, sdk.Privacy.PUBLIC)
    (page_node,) = doc.card.blocks
    grid = Image.new("RGB", expected.size)
    for index, item in enumerate(page_node.items):
        with Image.open(BytesIO(item.image.content)) as tile:
            grid.paste(
                tile,
                (
                    (index % page_node.columns) * page_node.cell_width,
                    (index // page_node.columns) * page_node.cell_height,
                ),
            )
    assert grid.tobytes() == expected.tobytes(), (
        "source pixels were omitted or reordered"
    )

    for partial in (
        [{"poster": dict(poster, tiles=poster["tiles"][:-1])}],
        parts + parts,
    ):
        try:
            await handler.document(None, title, partial, sdk.Privacy.PUBLIC)
        except adapter.UnsupportedProjection:
            pass
        else:
            raise AssertionError("partial or duplicate image admitted")
    renderer = load("presentation.command_card")
    fonts = load("presentation.bundled_font")

    def font_provider():
        with patch.object(fonts, "__file__", str(work / "bundled_font.py")):
            return fonts.BundledFontProvider()

    output = renderer.CommandCardRenderer(font_provider_factory=font_provider)(
        doc, audience=sdk.DisplayAudience.PUBLIC
    )
    layout = load("presentation.layout_renderer")
    pages = load("presentation.layout_dense").plan_dense(page_node)
    assert len(pages) == len(output.bitmaps) >= 2
    selected = layout.apply_theme_selection(doc.card, "warm_paper")
    cursor = 0
    results = []
    for planned, bitmap in zip(pages, output.bitmaps, strict=True):
        _, measured = layout._prepare(
            selected,
            layout.BitmapBounds(
                max_bytes=4 * 1024**2, max_chars=160_000, max_input_lines=6000
            ),
            font_provider(),
            planned,
        )
        y = int(
            next(box.bounds[1] for box in measured.boxes if box.kind == "collage_tile")
        )
        page = Image.open(BytesIO(bitmap.content)).convert("RGB")
        height = min(planned.height, expected.height - cursor)
        body = page.crop((0, y, expected.width, y + height))
        target = expected.crop((0, cursor, expected.width, cursor + height))
        # The Core rasterizer supersamples every tile 2x and downsamples the finished
        # page. Compare all content rows, excluding only the 3px antialias boundary.
        inner = (3, 3, expected.width - 3, height - 3)
        diff = ImageChops.difference(body.crop(inner), target.crop(inner))
        error = max(ImageStat.Stat(diff).mean)
        assert error < 6, (name, cursor, error)
        if name == "rows":
            # The low-frequency channel identifies every 251-row band across pages.
            x = expected.width // 2
            for row in range(3, height - 3):
                assert (
                    abs(body.getpixel((x, row))[1] - target.getpixel((x, row))[1]) < 4
                )
        cursor += height
        dest = work / f"{name}-page-{planned.index}.png"
        dest.write_bytes(bitmap.content)
        results.append(
            {
                "sha256": digest(bitmap.content),
                "width": page.width,
                "height": page.height,
                "source_rows": height,
                "max_channel_mean_error": round(error, 3),
                "bytes": len(bitmap.content),
            }
        )
    assert cursor == expected.height
    return {
        "input_sha256": digest(source_file.read_bytes()),
        "source": source.size,
        "content": expected.size,
        "min_scale": min(
            expected.width / source.width, expected.height / source.height
        ),
        "pages": results,
    }


async def webchat_stage(work, astrbot, core):
    from types import SimpleNamespace

    os.environ["ASTRBOT_ROOT"] = str(work)
    sys.path[:0] = [str(astrbot), str(core.parent)]
    from astrbot.api.event import MessageChain
    from astrbot.core.platform.sources.webchat import webchat_event

    ports = importlib.import_module(core.name + ".core.ports")
    message_port = importlib.import_module(core.name + ".adapters.astrbot.message_port")
    ref = ports.ConversationRef(
        "webchat", ports.ConversationKind.DIRECT, "fixture", "webchat"
    )
    target = ports.MessageTarget("fixture", None, ref)

    attachments = work / "attachments"
    attachments.mkdir()
    webchat_event.attachments_dir = str(attachments)
    received = []

    class Queue:
        async def put_back_queue(self, key, message):
            if message["type"] == "image":
                path = attachments / message["data"].removeprefix("[IMAGE]")
                received.append(digest(path.read_bytes()))
            return True

    webchat_event.webchat_queue_mgr = Queue()
    for name in ("rows", "help"):
        pages = tuple(
            page.read_bytes() for page in sorted(work.glob(name + "-page-*.png"))
        )
        digests = [digest(p) for p in pages]

        async def send(chain):
            await webchat_event.WebChatMessageEvent._send("fixture", chain, "fixture")

        event = SimpleNamespace(send=send, chain_result=MessageChain)
        ingress = SimpleNamespace(
            adapter_id="webchat",
            delivery_route="webchat",
            conversation_id="fixture",
            conversation_kind=ref.kind,
            actor_id="actor",
            evidence=SimpleNamespace(event=event),
        )
        port = message_port.AstrBotMessagePort(
            None, command_current=lambda value: value is ingress
        )
        prepared = port.prepare_command(
            ingress,
            target,
            ports.RenderedMessage("原图分页", png_pages=pages),
            image_current=lambda: None,
        )
        received.clear()
        await prepared.dispatch()
        assert received == digests
        (work / (name + "-received.json")).write_text(json.dumps(received))


async def main(args):
    from build_local import worker_archive
    from environments import selected

    profile = json.loads((ROOT / "profiles/lock.json").read_bytes())["genshin"]
    runtime_dir = ROOT / ".sayu-local"
    exe, _ = selected(
        runtime_dir / "genshin",
        (ROOT / "profiles/genshin-requirements.txt").read_bytes(),
    )
    source = runtime_dir / "runtime" / f"GenshinUID-{profile['snapshot_sha256']}.zip"
    assert digest(source.read_bytes()) == profile["snapshot_sha256"]
    sys.path[:0] = [str(args.core.parent), str(ROOT / "packages")]
    with tempfile.TemporaryDirectory(
        prefix="sayu-long-card-", ignore_cleanup_errors=True
    ) as tmp:
        work = Path(tmp)
        font = work / "assets/fonts/NotoSansCJKsc-Regular.otf"
        font.parent.mkdir(parents=True)
        font.write_bytes(
            (
                args.font or args.core / "presentation" / font.relative_to(work)
            ).read_bytes()
        )
        (work / "worker.pyz").write_bytes(worker_archive(profile, offline=True))
        with ZipFile(source) as archive:
            archive.extractall(work / "plugins")
        env = {
            k: v
            for k, v in os.environ.items()
            if k.upper() in {"SYSTEMROOT", "WINDIR", "LANG"}
        }
        for k in (
            "TMP",
            "TEMP",
            "TMPDIR",
            "HOME",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "XDG_CACHE_HOME",
        ):
            env[k] = str(work)
        subprocess.run(
            [exe, "-IB", "-X", "utf8", __file__, "--worker", str(work)],
            cwd=work,
            env=env,
            check=True,
            timeout=120,
        )
        report = {
            "worker_sha256": digest((work / "worker.pyz").read_bytes()),
            "plugin_sha256": profile["snapshot_sha256"],
            "font_sha256": digest(font.read_bytes()),
            "core_renderer_sha256": digest(
                (args.core / "presentation/layout_renderer.py").read_bytes()
            ),
            "core_message_port_sha256": digest(
                (args.core / "adapters/astrbot/message_port.py").read_bytes()
            ),
        }
        for name in ("rows", "help"):
            report[name] = await render_case(args.core, work, name)
        if args.native_help:
            native_digest = digest(args.native_help.read_bytes())
            assert report["help"]["input_sha256"] == native_digest
            report["native_help_sha256"] = native_digest
        if args.astrbot:
            report["webchat_source_sha256"] = digest(
                (
                    args.astrbot
                    / "astrbot/core/platform/sources/webchat/webchat_event.py"
                ).read_bytes()
            )
            subprocess.run(
                [
                    str(args.astrbot_python),
                    "-IB",
                    "-X",
                    "utf8",
                    __file__,
                    "--webchat",
                    str(work),
                    "--astrbot",
                    str(args.astrbot),
                    "--core",
                    str(args.core),
                ],
                cwd=work,
                env=env,
                check=True,
                timeout=120,
            )
            for name in ("rows", "help"):
                report[name]["webchat_queue"] = json.loads(
                    (work / (name + "-received.json")).read_bytes()
                )
        args.output.write_text(json.dumps(report, indent=2), encoding="utf8")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--core", type=Path)
    p.add_argument("--font", type=Path)
    p.add_argument(
        "--native-help",
        type=Path,
        help="independent seed-41 native GenshinUID help JPEG",
    )
    p.add_argument("--astrbot", type=Path)
    p.add_argument("--astrbot-python", type=Path)
    p.add_argument("--output", type=Path)
    p.add_argument("--worker", type=Path)
    p.add_argument("--webchat", type=Path)
    a = p.parse_args()
    if a.worker:
        asyncio.run(worker_stage(a.worker))
    elif a.webchat:
        asyncio.run(webchat_stage(a.webchat, a.astrbot, a.core))
    else:
        a.core = a.core.resolve()
        if a.astrbot:
            a.astrbot = a.astrbot.resolve()
            a.astrbot_python = a.astrbot_python.resolve()
        asyncio.run(main(a))
