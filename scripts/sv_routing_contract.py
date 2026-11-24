"""Check managed routing changes across two actual registrations of the same plugin."""

import argparse
import asyncio
from contextlib import asynccontextmanager, closing, nullcontext
from functools import partial
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/routing_fixture"
NAME = "routing_probe"
sys.path.insert(0, str(ROOT / "scripts"))
from native_contract import REFERENCE_COMMIT, copy_reference, offline  # noqa: E402


@asynccontextmanager
async def runtime(work, worker):
    if worker:
        sys.path.insert(0, str(worker))
        import sayu_compat
        from service_settings import management_scope
        from plugin_runtime import PluginRuntime

        source, data = work / "plugins", work / "data"
        sayu_compat.install(
            NAME, "old", data, source_root=source, runtime_mode="package"
        )
        with management_scope(writable=True):
            PluginRuntime(data).import_package(source, NAME)

        async def send(text):
            with management_scope():
                try:
                    result = await sayu_compat.dispatch(
                        text, actor_id="actor", conversation_kind="direct"
                    )
                except sayu_compat.NoMatch:
                    return []
                return [p["text"] for p in result]

        def persisted():
            with closing(sqlite3.connect(data / "services.sqlite3")) as db:
                return json.loads(
                    db.execute(
                        "SELECT payload FROM profile_service_config WHERE name=?",
                        (NAME,),
                    ).fetchone()[0]
                )

        yield send, partial(management_scope, writable=True), persisted
        return

    sys.path.insert(0, str(work))
    from gsuid_core.utils.database.base_models import init_database

    await init_database()
    from gsuid_core.utils.database import startup  # noqa: F401
    from gsuid_core.config import plugin_config_store
    from gsuid_core.server import (
        GsServer,
        core_start_before_execute,
        core_start_execute,
        core_shutdown_execute,
    )
    from gsuid_core.handler import decode_inbound_frame, handle_event
    from gsuid_core.bot import _Bot
    from starlette.websockets import WebSocketState

    server = GsServer()
    for name in (NAME, "routing_rival"):
        for entry in server.load_plugin(work / "gsuid_core/plugins" / name):
            server.cached_import(*entry)
    plugin_config_store.save_all()
    await core_start_before_execute()
    await core_start_execute()

    class Adapter:
        application_state = WebSocketState.CONNECTED

        def __init__(self):
            self.messages = []

        async def send_bytes(self, raw):
            self.messages.extend(
                p["data"] for p in json.loads(raw)["content"] if p["type"] == "text"
            )

    adapter = Adapter()
    bot = _Bot("offline-fixture", adapter)
    bot.start_send_worker()
    process = asyncio.create_task(bot._process())
    number = 0

    async def send(text):
        nonlocal number
        number += 1
        frame = {
            "bot_id": "contract",
            "bot_self_id": "fixture",
            "user_id": f"actor-{number}",
            "msg_id": str(number),
            "user_type": "direct",
            "user_pm": 6,
            "content": [{"type": "text", "data": text}],
        }
        event = decode_inbound_frame(json.dumps(frame).encode(), "offline-fixture")
        await handle_event(bot, event)
        await asyncio.wait_for(bot.queue.join(), 10)
        await asyncio.wait_for(bot._send_queue.join(), 10)
        values, adapter.messages = adapter.messages, []
        return values

    try:
        yield (
            send,
            nullcontext,
            lambda: json.loads(
                (work / "data/plugins_configs" / (NAME + ".json")).read_bytes()
            ),
        )
    finally:
        process.cancel()
        bot._send_task.cancel()
        await asyncio.gather(process, bot._send_task, return_exceptions=True)
        await core_shutdown_execute()


async def phase(work, worker, reload):
    sys.addaudithook(offline)
    async with runtime(work, worker) as (send, grant, persisted):
        from gsuid_core.config import plugin_config_store
        from gsuid_core.sv import get_plugin_available_prefix, get_plugin_prefixs
        from gsuid_core.trigger import registry_version

        f = importlib.import_module("plugins." + NAME)
        shared = {}
        special = {}

        async def check(tag, expected):
            actual = {text: await send(text) for text in expected}
            assert actual == expected, (tag, actual, expected)
            shared[tag] = actual

        async def rival(tag, expected):
            if not worker:
                observed = await send("inter")
                assert observed == [expected], (tag, observed)
                special[tag] = observed

        def disk():
            saved = persisted()
            return [
                saved["prefix"],
                saved["force_prefix"],
                saved["alias"],
                saved["disable_force_prefix"],
                saved["allow_empty_prefix"],
                saved["priority"],
                saved["sv"]["a"]["priority"],
            ]

        if reload:
            expected = [["new"], ["forced"], ["DeclaredAlias"], True, False, 0, 1]
            assert disk() == expected, disk()
            shared["restored"] = disk()
            await check(
                "rebuilt",
                {
                    "oldearly": [],
                    "newearly": ["early"],
                    "forcedearly": [],
                    "modforcedearly": [],
                    "early": [],
                    "newrace": ["A"],
                    "newtool_alias": [],
                    "bare": ["bare"],
                },
            )
            await rival("restarted_order", "A")
            with grant():
                f.plugin.set(False, prefix="str")
                assert (
                    get_plugin_prefixs(NAME) == "str"
                    and get_plugin_available_prefix(NAME) == "s"
                )
                try:
                    f.late("broken")
                except AttributeError:
                    shared["string_registration"] = "AttributeError"
                else:
                    raise AssertionError(
                        "string setter unexpectedly expanded as a list"
                    )
                # A newly constructed native SV normalizes the string again.
                f.extra_service()
                assert list(get_plugin_prefixs(NAME)) == ["str"]
                f.late("normalized")
                f.plugin.set(
                    False,
                    prefix=[],
                    allow_empty_prefix=False,
                    disable_force_prefix=True,
                )
                f.late("noprefix")
            await check(
                "empty_and_normalized",
                {
                    "strnormalized": ["normalized"],
                    "snormalized": [],
                    "noprefix": ["noprefix"],
                },
            )
            return {"shared": shared, "backend_specific": special}

        await check(
            "initial",
            {
                "oldearly": ["early"],
                "forcedearly": ["early"],
                "newearly": [],
                "early": [],
                "oldrace": ["B"],
                "bare": ["bare"],
                "oldbare": [],
                "oldtool_alias": [],
                "DeclaredAliasearly": [],
            },
        )
        await rival("initial_order", "rival")
        before = registry_version()
        with grant():
            f.plugin.set(
                prefix=["new"],
                force_prefix=["modforced"],
                allow_empty_prefix=True,
                alias=["RuntimeAlias"],
                priority=0,
            )
            assert registry_version() == before
            assert get_plugin_available_prefix(NAME) == "modforced"
            assert list(f.plugin.alias) == ["RuntimeAlias"]
            expected = [["old"], ["forced"], ["DeclaredAlias"], False, False, 5, 9]
            assert disk() == expected, disk()
            shared["lazy_disk"] = disk()
        await check(
            "existing_routes",
            {
                "oldearly": ["early"],
                "forcedearly": ["early"],
                "newearly": [],
                "early": [],
                "RuntimeAliasearly": [],
                "oldrace": ["B"],
            },
        )
        await rival("plugin_priority_only", "rival")
        with grant():
            f.late("late")
            assert registry_version() == before + 1
        await check(
            "new_routes",
            {
                "oldlate": [],
                "newlate": ["late"],
                "modforcedlate": ["late"],
                "late": ["late"],
            },
        )
        with grant():
            f.a.set(priority=1)
        await check("service_priority", {"oldrace": ["A"], "newrace": []})
        await rival("service_priority_order", "A")
        with grant():
            plugin_config_store.save_dirty()
            expected = [["new"], ["modforced"], ["RuntimeAlias"], False, True, 0, 1]
            assert disk() == expected, disk()
            shared["flushed"] = disk()
            try:
                f.a.set(False, prefix=["svonly"], alias=["ignored"])
            except ValueError as exc:
                assert worker and type(exc).__name__ == "RestrictedConfiguration"
                special["service_prefix"] = type(exc).__name__
            else:
                assert not worker and f.a.prefix == ["svonly"]
                special["service_prefix"] = "stored_unused"
            f.plugin.set(False, disable_force_prefix=True, allow_empty_prefix=False)
            f.late("last")
            assert get_plugin_available_prefix(NAME) == "new"
        await check(
            "force_and_empty_disabled",
            {
                "modforcedlast": [],
                "newlast": ["last"],
                "last": [],
                "modforcedlate": ["late"],
                "late": ["late"],
                "svonlylast": [],
            },
        )
        return {"shared": shared, "backend_specific": special}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path)
    parser.add_argument(
        "--worker", type=Path, help="optional prebuilt package-mode Worker archive"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--child", choices=["first", "reload"], help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        report = asyncio.run(phase(Path.cwd(), args.worker, args.child == "reload"))
    else:
        fixture_files = sorted(FIXTURE.glob("*/__init__.py"))
        report = {
            "reference_commit": REFERENCE_COMMIT,
            "fixtures": {
                f.parent.name: hashlib.sha256(f.read_bytes()).hexdigest()
                for f in fixture_files
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            worker = args.worker
            if worker is None:
                from build_local import worker_archive

                worker = temp / "worker.pyz"
                worker.write_bytes(worker_archive({"package": NAME, "prefix": "old"}))
            worker = worker.resolve()
            report["worker_sha256"] = hashlib.sha256(worker.read_bytes()).hexdigest()
            for mode in ("native", "loader"):
                work = temp / mode
                work.mkdir()
                plugin_root = work / "plugins"
                if mode == "native":
                    report["manifest"] = copy_reference(
                        (args.reference or ROOT / ".sayu-local/reference").resolve(),
                        work,
                    )
                    plugin_root = work / "gsuid_core/plugins"
                for f in fixture_files:
                    if mode == "native" or f.parent.name == NAME:
                        shutil.copytree(f.parent, plugin_root / f.parent.name)
                records = {}
                for label in ("first", "reload"):
                    path = work / (label + ".json")
                    cmd = [
                        sys.executable,
                        "-IB",
                        "-X",
                        "utf8",
                        str(Path(__file__).resolve()),
                        "--child",
                        label,
                        "--output",
                        str(path),
                    ]
                    if mode == "loader":
                        cmd += ["--worker", str(worker)]
                    subprocess.run(
                        cmd,
                        cwd=work,
                        check=True,
                        timeout=100,
                        env={**os.environ, "HOME": str(work), "USERPROFILE": str(work)},
                    )
                    records[label] = json.loads(path.read_bytes())
                report[mode] = records
        for label in ("first", "reload"):
            assert (
                report["native"][label]["shared"] == report["loader"][label]["shared"]
            )
    args.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf8"
    )


if __name__ == "__main__":
    main()
