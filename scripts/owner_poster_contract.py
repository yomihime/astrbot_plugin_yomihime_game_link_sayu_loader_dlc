"""Check owner-page delivery with separate worker, Core and AstrBot interpreters.

Use a fresh output directory. 'worker' accepts any tall baseline JPEG that fits
the half-resolution poster budget. For an actual handler, supply its exported
poster JSON directly to 'core'. An optional --record contains preprojection
metadata with ordered text and image summaries; --font selects the source face.
'webchat' checks the Core page manifest against real temporary queue attachments.
"""

import argparse
import asyncio
import base64
import hashlib
import importlib
import json
import os
import sys
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import ModuleType, SimpleNamespace

from PIL import Image, ImageChops, ImageStat, ImageFont, ImageDraw

ROOT = Path(__file__).resolve().parents[1]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def worker_case(jpeg, target):
    sys.path.insert(0, str(ROOT / "worker"))
    from images import prepare_owner_parts

    raw = jpeg.read_bytes()
    parts = prepare_owner_parts(
        [{"image": base64.b64encode(raw).decode("ascii")}], ROOT, "fixture"
    )
    target.write_text(json.dumps(parts), encoding="utf8")


async def check_delivery_failures(fixture_type, output_status):
    """A text fallback or an uncertain send never satisfies an image receipt."""
    for failure in ("page count", "total bytes", "timeout"):
        fixture = fixture_type()
        await fixture.asyncSetUp()
        try:
            if failure == "page count":
                fixture.bitmaps = (fixture.bitmaps[0],) * 9
            elif failure == "total bytes":
                # Inject an oversized renderer result at OutputService's boundary.
                bitmap = replace(fixture.bitmaps[0], content=b"x" * (4 * 1024**2))
                fixture.bitmaps = (bitmap,) * 5
            else:
                fixture.event.gate = asyncio.Event()
                fixture.fixture.output._send_timeout = 0.01
            outcome = await fixture.fixture.output.route(
                fixture.fixture.view, fixture.result
            )
            if failure == "timeout":
                assert outcome.status is output_status.UNKNOWN
                await fixture.event.entered.wait()
                assert len(fixture.event.calls) == 1 and not fixture.text_sends
                fixture.event.gate.set()
            else:
                assert outcome.status is output_status.SENT and not fixture.event.calls
                assert len(fixture.text_sends) == 1 and fixture.text_sends[0][1].text
        finally:
            await fixture.asyncTearDown()


async def core_case(core, jpeg, target, out, record, font):
    sys.path.insert(0, str(core))
    from tests.services.test_collage_output import (
        PrivateCollageOutputTests,
        _CachingScope,
    )
    from ygl_test_subject.presentation.command_card import CommandCardRenderer
    from ygl_test_subject.presentation import layout_renderer, layout_dense
    from ygl_test_subject.presentation.bundled_font import BundledFontProvider
    from ygl_test_subject.services.output import OutputStatus
    import yomihime_game_link_sdk as sdk

    sys.path.insert(0, str(ROOT / "packages"))
    sys.modules["sayu_loader._api_v2"] = sdk
    runtime = ModuleType("sayu_loader.sayu.runtime")
    runtime.WORKERS = {}
    sys.modules[runtime.__name__] = runtime
    adapter = importlib.import_module("sayu_loader.sayu.module")
    await check_delivery_failures(PrivateCollageOutputTests, OutputStatus)
    fixture = PrivateCollageOutputTests()
    await fixture.asyncSetUp()
    try:
        handler = adapter.Module("waves", None).handlers().capabilities["query"]
        assert handler.private
        parts = json.loads(target.read_bytes())
        poster = parts[0]["poster"]
        bad_last_tile = list(poster["tiles"])
        bad_last_tile[-1] = base64.b64encode(b"bad last page").decode("ascii")
        for bad in (
            parts + parts,
            [{"poster": dict(poster, tiles=bad_last_tile)}],
            [{"poster": dict(poster, tiles=poster["tiles"][:-1])}],
        ):
            with fixture.assertRaises(adapter.UnsupportedProjection):
                await handler.document(
                    fixture.fixture.view, "owner poster", bad, sdk.Privacy.PRIVATE
                )
        assert not fixture.event.calls
        public = adapter.Module("genshin", None).handlers().capabilities["query"]
        with fixture.assertRaises(adapter.UnsupportedProjection):
            await public.document(
                fixture.fixture.view, "owner poster", parts, sdk.Privacy.PRIVATE
            )
        with fixture.assertRaises(adapter.UnsupportedProjection):
            await handler.document(
                fixture.fixture.view, "owner poster", parts, sdk.Privacy.PUBLIC
            )
        doc = await handler.document(
            fixture.fixture.view, "owner poster", parts, sdk.Privacy.PRIVATE
        )
        assert doc.privacy is sdk.Privacy.PRIVATE
        result = replace(fixture.result, document=doc)
        assert result.privacy is sdk.Privacy.PRIVATE
        fixture.scope = _CachingScope()
        renderer = CommandCardRenderer()
        renders = []

        def render(document, **kwargs):
            assert kwargs["audience"].value == "private"
            output = renderer(document, **kwargs)
            if kwargs.get("image", True):
                renders.append(output)
            return output

        fixture.fixture.output._command_card_renderer = render
        outcome = await fixture.fixture.output.route(fixture.fixture.view, result)
        assert outcome.status is OutputStatus.SENT, (
            outcome.status,
            getattr(outcome, "error", None),
        )
        assert not fixture.text_sends and len(fixture.event.calls) == 1, (
            len(fixture.text_sends),
            len(fixture.event.calls),
        )
        frames = [
            base64.b64decode(i.file.removeprefix("base64://"))
            for i in fixture.event.calls[0]
        ]
        assert len(renders) == 1 and frames == [p.content for p in renders[0].bitmaps]
        assert len(frames) == len(fixture.scope.contents) == fixture.scope.stores
        assert frames == list(fixture.scope.contents.values())
        assert 1 < len(frames) <= 8 and sum(map(len, frames)) <= 16 * 1024 * 1024
        with Image.open(jpeg) as src:
            size = src.size
            src.draft("RGB", (src.width // 2, src.height // 2))
            expected = src.convert("RGB")
        raster_size = expected.size
        if record is not None:
            rawparts = [
                part
                for block in json.loads(record.read_bytes())["preprojection"]
                for part in block
            ]
            face = ImageFont.truetype(font, 24)
            height = sum(face.getmetrics()) + 6 + 32
            canvas = Image.new(
                "RGB",
                (expected.width, expected.height + height * (len(rawparts) - 1)),
                "white",
            )
            draw = ImageDraw.Draw(canvas)
            y = 0
            for item in rawparts:
                if item["kind"] == "image":
                    assert sha(jpeg.read_bytes()) == item["sha256"]
                    canvas.paste(expected, (0, y))
                    y += expected.height
                else:
                    assert (
                        item["kind"] == "text"
                        and "\n" not in item["text"]
                        and face.getlength(item["text"]) < canvas.width - 32
                    )
                    draw.text(
                        (16, y + 16), item["text"], font=face, fill="black", anchor="la"
                    )
                    y += height
            assert y == canvas.height
            expected = canvas
        assert expected.size == (
            parts[0]["poster"]["width"],
            parts[0]["poster"]["height"],
        )
        node = doc.card.blocks[0]
        checked = Image.new("RGB", expected.size)
        for rank, item in enumerate(node.items):
            with Image.open(BytesIO(item.image.content)) as tile:
                checked.paste(
                    tile,
                    (
                        rank % node.columns * node.cell_width,
                        rank // node.columns * node.cell_height,
                    ),
                )
        assert ImageChops.difference(checked, expected).getbbox() is None
        pages = layout_dense.plan_dense(node)
        assert len(frames) == len(pages)
        cursor = 0
        results = []
        out.mkdir(parents=True, exist_ok=True)
        selected = layout_renderer.apply_theme_selection(doc.card, "warm_paper")
        for f, pageinfo in zip(frames, pages, strict=True):
            _, measured = layout_renderer._prepare(
                selected,
                layout_renderer.BitmapBounds(
                    max_bytes=4 * 1024**2, max_chars=160000, max_input_lines=6000
                ),
                BundledFontProvider(),
                pageinfo,
            )
            top = next(
                box.bounds[1] for box in measured.boxes if box.kind == "collage_tile"
            )
            rows = min(pageinfo.height, expected.height - cursor)
            with Image.open(BytesIO(f)) as page:
                assert (
                    len(f) <= 4 * 1024 * 1024
                    and page.width * page.height <= (128 * 2**20) // 36
                )
                body = page.convert("RGB").crop((0, top, expected.width, top + rows))
                ref = expected.crop((0, cursor, expected.width, cursor + rows))
                err = max(ImageStat.Stat(ImageChops.difference(body, ref)).mean)
                assert err < 12, (pageinfo.index, err)
                results.append(
                    {
                        "page": pageinfo.index,
                        "rows": rows,
                        "sha256": sha(f),
                        "bytes": len(f),
                        "size": page.size,
                        "max_channel_mean_error": round(err, 3),
                    }
                )
            (out / f"page-{pageinfo.index}.png").write_bytes(f)
            cursor += rows
        assert (
            cursor == expected.height
            and min(raster_size[0] / size[0], raster_size[1] / size[1]) >= 0.5
        )
        (out / "page-digests.json").write_text(
            json.dumps([sha(frame) for frame in frames]), encoding="utf8"
        )
        print(
            json.dumps(
                {
                    "source": sha(jpeg.read_bytes()),
                    "source_size": size,
                    "decoded": raster_size,
                    "canvas": expected.size,
                    "tile_count": len(node.items),
                    "tiles_bytes": sum(len(i.image.content) for i in node.items),
                    "output_total": sum(map(len, frames)),
                    "private_user_cache_pages": fixture.scope.stores,
                    "exact_event_sends": 1,
                    "failure_checks": {
                        "over_8_pages": "text_only",
                        "over_16_MiB": "text_only",
                        "send_timeout": "unknown",
                    },
                    "pages": results,
                },
                indent=2,
            )
        )
    finally:
        await fixture.asyncTearDown()


async def webchat_case(astrbot, core, page_root):
    # This is the production adapter with an isolated official request queue.
    # It sends no HTTP request; there is no Dashboard user or online history.
    os.environ["ASTRBOT_ROOT"] = str(page_root.resolve())
    sys.path[:0] = [str(astrbot.resolve()), str(core.resolve().parent)]
    from astrbot.api.event import MessageChain
    from astrbot.core.platform.sources.webchat import webchat_event
    from astrbot.core.platform.sources.webchat.webchat_queue_mgr import WebChatQueueMgr

    ports = importlib.import_module(core.name + ".core.ports")
    port_module = importlib.import_module(core.name + ".adapters.astrbot.message_port")
    ref = ports.ConversationRef(
        "webchat", ports.ConversationKind.DIRECT, "private-session", "webchat"
    )
    target = ports.MessageTarget(ref.conversation_id, None, ref)
    attachments = page_root / "observed-attachments"
    attachments.mkdir(exist_ok=True)
    webchat_event.attachments_dir = str(attachments)
    webchat_event.webchat_queue_mgr = WebChatQueueMgr()
    q = webchat_event.webchat_queue_mgr.get_or_create_back_queue(
        "private-request", "private-session"
    )

    async def send(chain):
        return await webchat_event.WebChatMessageEvent._send(
            "private-request", chain, "private-session"
        )

    event = SimpleNamespace(chain_result=MessageChain, send=send)
    ingress = SimpleNamespace(
        adapter_id="webchat",
        delivery_route="webchat",
        conversation_id=ref.conversation_id,
        conversation_kind=ref.kind,
        actor_id="private-owner",
        evidence=SimpleNamespace(event=event),
    )
    port = port_module.AstrBotMessagePort(None, command_current=lambda v: v is ingress)
    manifest = json.loads((page_root / "page-digests.json").read_bytes())
    assert 1 < len(manifest) <= 8
    pages = tuple(
        (page_root / f"page-{i}.png").read_bytes() for i in range(1, len(manifest) + 1)
    )
    assert [sha(page) for page in pages] == manifest
    prepared = port.prepare_command(
        ingress,
        target,
        ports.RenderedMessage("private poster", png_pages=pages),
        image_current=lambda: None,
    )
    await prepared.dispatch()
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    assert [e["type"] for e in events] == ["image"] * len(pages)
    assert len({e["message_id"] for e in events}) == 1
    digests = [
        sha((attachments / e["data"].removeprefix("[IMAGE]")).read_bytes())
        for e in events
    ]
    assert digests == [sha(p) for p in pages]
    print(
        json.dumps(
            {
                "adapter": "WebChatMessageEvent._send",
                "queue": "WebChatQueueMgr",
                "images": digests,
                "same_message_id": True,
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    stages = parser.add_subparsers(dest="stage", required=True)
    worker = stages.add_parser("worker", help="project a single JPEG to worker parts")
    worker.add_argument("jpeg", type=Path)
    worker.add_argument("parts", type=Path)
    core = stages.add_parser("core", help="render and verify a complete owner result")
    core.add_argument("jpeg", type=Path)
    core.add_argument("parts", type=Path)
    core.add_argument("--core", type=Path, required=True)
    core.add_argument("--out", type=Path, required=True)
    core.add_argument("--record", type=Path)
    core.add_argument(
        "--font", default="msyh.ttc", help="same font as the captured worker"
    )
    webchat = stages.add_parser(
        "webchat", help="check actual ordered temporary attachments"
    )
    webchat.add_argument("--core", type=Path, required=True)
    webchat.add_argument("--astrbot", type=Path, required=True)
    webchat.add_argument(
        "--out", type=Path, required=True, help="directory written by the Core stage"
    )
    args = parser.parse_args()
    if args.stage == "worker":
        worker_case(args.jpeg, args.parts)
    elif args.stage == "webchat":
        asyncio.run(webchat_case(args.astrbot, args.core, args.out))
    else:
        asyncio.run(
            core_case(
                args.core.resolve(),
                args.jpeg,
                args.parts,
                args.out,
                args.record,
                args.font,
            )
        )
