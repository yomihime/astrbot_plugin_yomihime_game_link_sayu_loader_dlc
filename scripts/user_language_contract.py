"""Compare CoreUser language storage and event-local translation across two generations."""

import argparse
import asyncio
from contextlib import nullcontext
from functools import partial
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build_local import worker_archive  # noqa: E402
from native_contract import REFERENCE_COMMIT, copy_reference, offline  # noqa: E402


async def check(mode, phase, worker):
    sys.addaudithook(offline)
    native = mode == "native"
    work = Path.cwd()
    admin = nullcontext
    if native:
        sys.path.insert(0, str(work))
        from gsuid_core.utils.database import base_models as db

        await db.init_database()
        from gsuid_core.utils.database.startup import create_core_tables

        await create_core_tables()
    else:
        sys.path.insert(0, str(worker))
        from sayu_compat import install
        from plugin_runtime import PluginRuntime
        from service_settings import management_scope

        data = work / "private"
        install("lang_probe", "", data, source_root=work)
        runtime = PluginRuntime(data)
        runtime.install(work, "lang_probe")
        admin = partial(management_scope, writable=True)
        with admin():
            async with runtime.orm.startup():
                await runtime.orm.migrate()

    from gsuid_core.bot import Bot
    from gsuid_core.config import core_config
    from gsuid_core.models import Event
    from gsuid_core.utils.database.models import CoreUser

    def actor(name="actor", adapter="astrbot", private=True, writable=False):
        if native:
            return nullcontext()
        from plugin_bindings import binding_scope

        return binding_scope(name, adapter, private, writable=writable)

    def make_bot(name="actor", adapter="astrbot"):
        return Bot(None, Event(user_id=name, bot_id=adapter, user_type="direct"))

    report = {}
    with admin():
        core_config.set_config("LANGUAGE", "en")
    if phase == 1:
        with actor(writable=True):
            report["empty"] = await CoreUser.get_user_lang("actor")
            report["update_missing"] = await CoreUser.set_user_lang(
                "actor", "astrbot", "ja"
            )
            report["still_empty"] = await CoreUser.get_user_lang("actor")
            report["rows"] = [
                await CoreUser.insert_user("astrbot", "actor", room, "Reader", "icon")
                for room in ("room1", "room2", "room1")
            ]
            bot = make_bot()
            report["old_bot"] = await bot.get_lang()
            report["set"] = await CoreUser.set_user_lang("actor", "astrbot", "ja")
            report["cached_bot"] = await bot.get_lang()
            fresh = make_bot()
            report["new_bot"] = await fresh.get_lang()
            report["translated"] = await fresh.t(
                "server.bot_connected", bot_id="sample"
            )
            report["stored"] = [
                r.language
                for r in await CoreUser.select_rows(user_id="actor", bot_id="astrbot")
            ]
        with actor("other", writable=True):
            await CoreUser.insert_user("astrbot", "other", None, None, None)
            report["other_actor"] = await make_bot("other").get_lang()
        assert report["empty"] == report["still_empty"] == report["cached_bot"] == "en"
        assert report["new_bot"] == "ja" and report["stored"] == ["ja", "ja"]
    else:
        with actor(writable=True):
            bot = make_bot()
            report["reopened"] = await bot.get_lang()
            report["update_invalid"] = await CoreUser.set_user_lang(
                "actor", "astrbot", "unavailable"
            )
            report["saved_invalid"] = (
                await CoreUser.base_select_data(user_id="actor", bot_id="astrbot")
            ).language
            report["global_for_invalid"] = await make_bot().get_lang()
            report["cleared"] = await CoreUser.set_user_lang("actor", "astrbot", None)
            report["global_for_clear"] = await make_bot().get_lang()
        assert report["reopened"] == "ja" and report["saved_invalid"] == "unavailable"
        assert report["global_for_invalid"] == report["global_for_clear"] == "en"
    result = {"shared": report}
    if native:
        await db.engine.dispose()
    elif phase == 1:
        from port_errors import Unsupported

        denied = []

        async def reject(name, awaitable):
            try:
                await awaitable
            except Unsupported:
                denied.append(name)
            else:
                raise AssertionError(f"missing rejection for {name}")

        with actor():
            assert await CoreUser.get_user_lang("actor") == "ja"
            await reject(
                "readonly", CoreUser.set_user_lang("actor", "astrbot", "zh-cn")
            )
            await reject("other_user", CoreUser.get_user_lang("other"))
            await reject("other_adapter", CoreUser.get_user_lang("actor", "elsewhere"))
            await reject("task", asyncio.create_task(make_bot().get_lang()))
        with actor(private=False):
            await reject("public_pref", CoreUser.get_user_lang("actor"))
            assert await fresh.get_lang() == "en"
        with actor("actor", adapter="other"):
            assert await make_bot("actor", "other").get_lang() == "en"
        assert await fresh.get_lang() == "en"
        result["guard"] = denied
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loader-python", type=Path, default=Path(sys.executable))
    parser.add_argument(
        "--reference", type=Path, default=ROOT / ".sayu-local/reference"
    )
    parser.add_argument("--child", choices=("loader", "native"))
    parser.add_argument("--phase", type=int)
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.child:
        report = asyncio.run(check(args.child, args.phase, args.worker))
    else:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            sources = copy_reference(args.reference.resolve(), work / "native")
            (work / "loader").mkdir()
            worker = work / "worker.pyz"
            worker.write_bytes(
                worker_archive(
                    json.loads((ROOT / "profiles/lock.json").read_bytes())["waves"]
                )
            )
            report = {
                "reference": REFERENCE_COMMIT,
                "sources": sources,
                "worker_sha256": hashlib.sha256(worker.read_bytes()).hexdigest(),
            }
            for mode, exe in (
                ("native", sys.executable),
                ("loader", args.loader_python.resolve()),
            ):
                observations = []
                for phase in (1, 2):
                    output = work / f"{mode}-{phase}.json"
                    subprocess.run(
                        [
                            exe,
                            "-IB",
                            "-X",
                            "utf8",
                            __file__,
                            "--child",
                            mode,
                            "--worker",
                            str(worker),
                            "--phase",
                            str(phase),
                            "--output",
                            str(output),
                        ],
                        cwd=work / mode,
                        check=True,
                        timeout=60,
                    )
                    observations.append(json.loads(output.read_bytes()))
                report[mode] = observations
        assert [r["shared"] for r in report["native"]] == [
            r["shared"] for r in report["loader"]
        ]
    args.output.write_text(json.dumps(report, indent=2), encoding="utf8")


if __name__ == "__main__":
    main()
