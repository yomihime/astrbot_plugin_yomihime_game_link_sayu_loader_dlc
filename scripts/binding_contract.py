"""Compare profile-local bindings with the same operations in a temporary native DB."""

import argparse
import asyncio
from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
ACTOR = "fixture-actor"
BOT = "fixture-adapter"


async def probe(mode, phase):
    sys.path.insert(0, str(ROOT / "scripts"))
    from native_contract import offline

    sys.addaudithook(offline)
    if mode == "loader":
        sys.path.insert(0, str(ROOT / "worker"))
        from plugin_bindings import binding_scope, install
        import sayu_compat

        data = Path.cwd() / "data"
        data.mkdir(exist_ok=True)
        sayu_compat.install("Example", "ex", data)
        install(sayu_compat.package, data)
        scope = binding_scope(ACTOR, BOT, True, writable=True)
    else:
        sys.path.insert(0, str(Path.cwd()))
        from gsuid_core.utils.database import base_models
        from gsuid_core.utils.database.models import GsBind

        await base_models.init_database()
        async with base_models.engine.begin() as connection:
            await connection.run_sync(GsBind.__table__.create, checkfirst=True)
        scope = nullcontext()
    from gsuid_core.utils.database.models import GsBind
    from gsuid_core.utils.database.api import get_uid

    result = {}
    with scope:
        if phase == "write":
            result["insert"] = [
                await GsBind.insert_uid(ACTOR, BOT, value, "room", 9)
                for value in ["0", "notnumber", "000000001", "000000002", "000000001"]
            ]
            result["sr"] = await GsBind.insert_uid(
                ACTOR, BOT, "000000003", "room", 9, game_name="sr"
            )
            result["switch_missing"] = await GsBind.switch_uid_by_game(
                ACTOR, BOT, "000000004"
            )
            result["rotate"] = await GsBind.switch_uid_by_game(ACTOR, BOT)
            result["set_group"] = await GsBind.update_data(ACTOR, BOT, group_id="room2")
        else:
            result["delete_missing"] = await GsBind.delete_uid(ACTOR, BOT, "000000004")
            result["delete"] = await GsBind.delete_uid(ACTOR, BOT, "000000001")
            result["switch_last"] = await GsBind.switch_uid_by_game(ACTOR, BOT)
        result["list"] = await GsBind.get_uid_list_by_game(ACTOR, BOT)
        result["sr_value"] = await GsBind.get_uid_by_game(ACTOR, BOT, "sr")
        result["group"] = (await GsBind.select_data(ACTOR, BOT)).group_id
        event = SimpleNamespace(text="", user_id=ACTOR, at=None, bot_id=BOT)
        result["stored"] = await get_uid(None, event, GsBind, None, True)
        event.text = "input 000000007"
        result["explicit"] = await get_uid(None, event, GsBind, None, True)
    if mode == "loader":
        with binding_scope(ACTOR, BOT, True):
            await GsBind.get_uid_by_game(ACTOR, BOT)
            try:
                await GsBind.delete_uid(ACTOR, BOT, "000000002")
            except NotImplementedError:
                pass
            else:
                raise AssertionError("read-only route permitted a UID mutation")
        for private, actor, adapter in [
            (False, ACTOR, BOT),
            (True, "other", BOT),
            (True, ACTOR, "other"),
            (True, "", BOT),
        ]:
            with binding_scope(actor, adapter, private):
                for operation in (
                    GsBind.get_uid_by_game(ACTOR, BOT),
                    GsBind.delete_uid(ACTOR, BOT, "000000002"),
                ):
                    try:
                        await operation
                    except NotImplementedError:
                        pass
                    else:
                        raise AssertionError("owner data escaped its invocation scope")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--mode", choices=["native", "loader"], help=argparse.SUPPRESS)
    parser.add_argument("--phase", choices=["write", "reopen"], help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.mode:
        result = asyncio.run(probe(args.mode, args.phase))
    else:
        from native_contract import REFERENCE_COMMIT, copy_reference

        result = {"reference": REFERENCE_COMMIT}
        with tempfile.TemporaryDirectory(prefix="sayu-bindings-") as folder:
            for mode in ["native", "loader"]:
                work = Path(folder) / mode
                work.mkdir()
                if mode == "native":
                    copy_reference(args.reference.resolve(), work)
                phases = []
                for phase in ["write", "reopen"]:
                    log = work / (phase + ".json")
                    command = [
                        sys.executable,
                        "-I",
                        "-B",
                        "-X",
                        "utf8",
                        str(Path(__file__).resolve()),
                        "--mode",
                        mode,
                        "--phase",
                        phase,
                        "--output",
                        str(log),
                    ]
                    env = {
                        k: v
                        for k, v in os.environ.items()
                        if k.upper() in {"SYSTEMROOT", "WINDIR", "LANG"}
                    }
                    env.update(TMP=str(work), TEMP=str(work), TMPDIR=str(work))
                    subprocess.run(command, cwd=work, env=env, check=True, timeout=45)
                    phases.append(json.loads(log.read_bytes()))
                result[mode] = phases
        if result["native"] != result["loader"]:
            raise AssertionError(result)
        result["owner_denials_verified"] = True
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
