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

from build_local import ROOT, ROOT_NAME


async def check(core, cases, list_commands=False):
    sys.path.insert(0, str(core.parent))

    def load(name):
        return importlib.import_module(core.name + "." + name)

    protocol = load("extensions.hot_update_protocol")
    sdk = load("yomihime_game_link_sdk")
    validate = load("core.contracts.validation_boundary").validate_contract
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
            message = doc.ordered_blocks[0].text
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

        bridge = importlib.import_module("sayu_loader.sayu.module")
        for route, module in modules.items():
            with tempfile.TemporaryDirectory(prefix="sayu-inventory-") as directory:
                inventory = await asyncio.wait_for(
                    bridge.exchange(
                        bridge.WORKERS[module.key][0],
                        Path(module.generation.name),
                        {"op": "inventory", "list_triggers": list_commands},
                        directory,
                    ),
                    30,
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
        assert mixed.ordered_blocks[0].text == "first\n\nlast"
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
        with tempfile.TemporaryDirectory(prefix="sayu-owner-") as work:
            direct = await bridge.exchange(
                bridge.WORKERS[genshin.key][0],
                Path(genshin.generation.name),
                {
                    "text": cases["sayugs"][0],
                    "event": genshin.event(None, True),
                    "private_image": True,
                },
                work,
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
