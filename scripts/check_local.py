"""Check a built candidate against a local Core SDK, outside a running Host."""

import argparse
import asyncio
import base64
import hashlib
import importlib
import json
import sys
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from build_local import ROOT, ROOT_NAME


async def check(core, cases, list_commands=False):
    sys.path.insert(0, str(core.parent))

    def load(name):
        return importlib.import_module(core.name + "." + name)

    protocol = load("extensions.hot_update_protocol")
    sdk = load("yomihime_game_link_sdk")
    validate = load("core.contracts.validation_boundary").validate_contract
    rendering = load("presentation.rendering")
    renderer = rendering.GenericDisplayRenderer(
        rendering.RenderingBounds(3000, 100, 32, 50, 1, 1, 1, 32, 8)
    )

    async def rendered_text(doc):
        audience = sdk.DisplayAudience(doc.privacy.value)
        output = await renderer.render(
            doc, limits=sdk.DisplayLimits(2, 4096), audience=audience
        )
        prefix = f"{doc.title}\n{doc.subject}\n"
        assert output.text.startswith(prefix)
        return output.text[len(prefix) :]

    with tempfile.TemporaryDirectory(prefix="sayu-check-") as temp:
        with zipfile.ZipFile(ROOT / "dist" / (ROOT_NAME + ".zip")) as candidate:
            release = protocol.parse_release(candidate.read("release.json"))
            for row in release["files"]:
                assert (
                    hashlib.sha256(candidate.read(row["path"])).hexdigest()
                    == row["sha256"]
                )
            descriptor = protocol.parse_plugin_descriptor(
                candidate.read("ygl-plugin.json")
            )
            envelope = protocol.parse_package_manifest(
                candidate.read(descriptor["manifest"]), host_install=True
            )
            manifest = load("extensions.disk_manifest").parse_manifest(
                protocol.b05_manifest_bytes(envelope)
            )
            candidate.extractall(temp)
        sys.path.insert(0, str(Path(temp) / "packages"))
        sys.modules["sayu_loader._api_v2"] = sdk
        runtime = importlib.import_module("sayu_loader.sayu.runtime")
        runtime.WORKERS = {
            key: (*binding[:-1], str(Path(temp) / "test-data" / key))
            for key, binding in runtime.WORKERS.items()
        }
        manifests = {m.route: m for m in manifest.modules}
        modules = {}
        handlers = {}

        async def start(route):
            meta = manifests[route]
            mod, factory = meta.factory_entry.split(":")
            instance = await getattr(
                importlib.import_module("sayu_loader." + mod), factory
            )().create(None)
            await instance.start()
            ports = instance.handlers().capabilities
            assert set(ports) == {cmd.capability_id for cmd in meta.commands}
            modules[route] = instance
            handlers[route] = ports["query"]

        async def invoke(route, text):
            result = validate(await handlers[route].invoke(None, {"text": text}))
            assert result.status == sdk.ResultStatus.SUCCESS, (route, result.error)
            return result.document

        async def verify(route, text, expected):
            doc = await invoke(route, text)
            if doc.card:
                raw = doc.card.blocks[0].asset.content
                assert hashlib.sha256(raw).hexdigest() == expected
                renderer = load("presentation.command_card").CommandCardRenderer()
                preview = renderer(doc, audience=sdk.DisplayAudience.PUBLIC)
                if not preview.bitmap:
                    presentation = load("presentation.command_card")
                    presentation.render_document(
                        presentation.apply_theme_selection(doc.card, "warm_paper"),
                        font_provider=presentation.BundledFontProvider(),
                    )
                    raise AssertionError("Core renderer returned only fallback text")
                return f"{route} JPEG bytes={len(raw)} sha256={expected}, Core PNG={len(preview.bitmap.content)} bytes"
            message = await rendered_text(doc)
            assert expected in message, (route, message)
            if route == "sayuww":
                assert doc.privacy == sdk.Privacy.PRIVATE
                digest = hashlib.sha256(message.encode()).hexdigest()
                assert (
                    digest
                    == "d5e492abb7eee0f4a8971c135f8f4df3d54d081565f20ca12713d3b6977889c4"
                )
                return f"{route} {message.splitlines()[0]}, {len(message)} chars, sha256={digest}"
            return f"{route} {message}"

        for route in cases:
            await start(route)

        for route, module in modules.items():
            inventory = await asyncio.wait_for(
                module.exchange({"op": "inventory", "list_triggers": list_commands}), 30
            )
            counts = inventory["registered"]
            assert counts["handlers"] > 1 and counts["triggers"] >= counts["handlers"]
            print(
                route,
                counts,
                "skipped declarations",
                sum(inventory["skipped"].values()),
            )

            if list_commands:
                for trigger in inventory["triggers"]:
                    print(route, json.dumps(trigger, ensure_ascii=False))
        if list_commands:
            for module in modules.values():
                await module.stop()
            return

        # These SDK boundary checks use synthetic messages, never blind samples.
        public = handlers["sayugs"]
        bitmap = b"\x89PNG\r\n\x1a\nfixture"
        encoded = base64.b64encode(bitmap).decode()
        mixed = await public.document(
            None,
            "test",
            [{"text": "first"}, {"image": encoded}, {"text": "last"}],
            sdk.Privacy.PUBLIC,
        )
        assert mixed.card.blocks[0].content == "first"
        assert mixed.card.blocks[1].asset.content == bitmap
        assert mixed.card.blocks[2].content == "last"
        assert await rendered_text(mixed) == "first\n\nlast\n\n图片未显示。"
        assignments = "委托一\n委托二\n委托三\n委托四"
        for privacy in (sdk.Privacy.PRIVATE, sdk.Privacy.PUBLIC):
            for body in (
                assignments,
                "\n".join(f"line {i}" if i % 4 else "" for i in range(1, 140)),
            ):
                doc = await public.document(None, "test", [{"text": body}], privacy)
                assert await rendered_text(doc) == body
        with patch.object(
            public.module,
            "exchange",
            AsyncMock(return_value={"parts": [{"text": "line\n" * 199}]}),
        ):
            too_many = await public.invoke(None, {"text": "test"})
        assert (
            too_many.status is sdk.ResultStatus.ERROR
            and too_many.error.code is sdk.ErrorCode.UNSUPPORTED
        )
        extra_images = {"parts": [{"image": encoded}, {"image": encoded}]}
        with patch.object(
            public.module, "exchange", AsyncMock(return_value=extra_images)
        ):
            rejected = await public.invoke(None, {"text": "test"})
        assert rejected.status is sdk.ResultStatus.ERROR
        assert rejected.error.code is sdk.ErrorCode.UNSUPPORTED
        ctx = SimpleNamespace(
            actor_id="test-user",
            invocation_id="event",
            conversation_id="room",
            conversation_kind="group",
        )
        event = modules["sayugs"].event(ctx)
        assert all(event.values()) and event == modules["sayugs"].event(ctx)
        assert event["actor_id"] != ctx.actor_id
        assert event != modules["sayusr"].event(ctx)
        ctx.conversation_kind = None
        assert not modules["sayugs"].event(ctx)["group_id"]
        print(
            *await asyncio.gather(
                *(verify(route, *args) for route, args in cases.items())
            ),
            sep="\n",
        )
        # Exercise the owner transport against the same upstream JPEG. The public
        # card path retains the original bytes; the private path requires a PNG.
        genshin = modules["sayugs"]
        direct = await genshin.exchange(
            {
                "text": cases["sayugs"][0],
                "event": genshin.event(None, True),
                "private_image": True,
            }
        )
        png = base64.b64decode(direct["parts"][0]["image"], validate=True)
        assert png.startswith(b"\x89PNG\r\n\x1a\n") and len(png) <= 4 * 1024 * 1024
        for route, text in (
            ("sayusr", "gs伤害乘区"),
            ("sayugs", "ww抽卡帮助"),
            ("sayuww", "sr三月七别名"),
        ):
            negative = await handlers[route].invoke(None, {"text": text})
            assert negative.status == sdk.ResultStatus.ERROR
        # A stopped generation must reject its existing handler. Its sibling still works.
        retired = handlers["sayusr"]
        old_tree = Path(modules["sayusr"].generation.name)
        await modules["sayusr"].stop()
        assert not old_tree.exists()
        try:
            await retired.invoke(None, {"text": cases["sayusr"][0]})
        except sdk.ServiceUnavailable:
            pass
        else:
            raise AssertionError("retired handler accepted a command")
        await verify("sayugs", *cases["sayugs"])
        await start("sayusr")
        await verify("sayusr", *cases["sayusr"])
        for module in modules.values():
            await module.stop()
        print(
            "SDK lifecycle: retired handler rejected, sibling remained available, replacement returned one reply"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", type=Path, required=True)
    parser.add_argument(
        "--list-commands",
        action="store_true",
        help="List the built candidate's upstream registrations without dispatching any chat command.",
    )
    args = parser.parse_args()
    cases = json.loads((ROOT / "profiles/cases.json").read_bytes())
    asyncio.run(check(args.core.resolve(), cases, args.list_commands))
