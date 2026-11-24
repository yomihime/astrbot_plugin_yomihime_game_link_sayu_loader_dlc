"""Exercise subscription persistence against the native reference and the private worker."""

import argparse
import asyncio
from contextlib import nullcontext
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build_local import worker_archive  # noqa: E402
from environments import selected  # noqa: E402
from native_contract import REFERENCE_COMMIT, copy_reference, offline  # noqa: E402


def event(kind, group="room"):
    return SimpleNamespace(
        user_id="actor",
        bot_id="adapter",
        bot_self_id="self",
        WS_BOT_ID="ws",
        user_type=kind,
        group_id=group,
        msg_id="m",
    )


async def cases(manager, phase):
    group = event("group")
    direct = event("direct", None)
    if phase == "write":
        await manager.add_subscribe("session", "test", group, "first", "1", "data")
        await manager.add_subscribe("session", "test", group, "second", "1")
        await manager.add_subscribe("single", "test", direct, uid="2")
        await manager.add_subscribe("single", "remove", direct)
        await manager.update_subscribe_message(
            "session", "test", group, "final", uid="1"
        )
        await manager.update_subscribe_data(
            "single", "test", direct, "payload", uid="2"
        )
        await manager.delete_subscribe("single", "remove", direct)
    rows = await manager.get_subscribe("test")

    async def task(uid):
        return "失败" if uid == "1" else "ok"

    priv, groups = await manager.muti_task(rows, task)
    return {
        "rows": [[r.uid, r.extra_message, r.extra_data] for r in rows],
        "removed": len(await manager.get_subscribe("remove")),
        "filtered": len(
            await manager.get_subscribe(
                "test", "actor", "adapter", "group", uid="1", WS_BOT_ID="ws"
            )
        ),
        "by_uid": sorted(await manager._to_dict(rows)),
        "private": {key: value["im"] for key, value in priv.items()},
        "groups": {
            key: [
                v["success"],
                v["fail"],
                [(m.type, m.data) for m in v["push_message"]],
            ]
            for key, v in groups.items()
        },
    }


async def child(args):
    sys.addaudithook(offline)
    here = Path.cwd()
    if args.mode == "native":
        sys.path.insert(0, str(here))
        from gsuid_core.utils.database import base_models

        await base_models.init_database()
        from gsuid_core.utils.database import startup

        await startup.create_core_tables()
        from gsuid_core.subscribe import gs_subscribe

        return {"observations": await cases(gs_subscribe, args.phase)}
    sys.path.insert(0, str(args.archive))
    from plugin_bindings import binding_scope
    from plugin_orm import install_orm
    from plugin_subscribe import install_subscribe
    from port_errors import Unsupported
    from sayu_compat import install, package
    from service_settings import management_scope
    from target_transport import BoundTarget, UnsupportedMessage

    install("StarRailUID", "sr", here / "private", source_root=here)
    from plugin_bindings import install as install_bindings

    install_bindings(package, here / "private")
    orm = install_orm(package, here / "private", "StarRailUID")
    manager = install_subscribe(package, orm)
    with management_scope(writable=True):
        async with orm.startup():
            await orm.migrate()

    async def denied(awaitable):
        try:
            await awaitable
        except (Unsupported, UnsupportedMessage):
            return
        raise AssertionError("unexpected access")

    with binding_scope("actor", "adapter", True, writable=args.phase == "write"):
        results = await cases(manager, args.phase)
        rows = await manager.get_subscribe("test")
        await denied(rows[0].send("unbound"))
        await denied(manager.get_subscribe("test", "somebody", "adapter", "direct"))
        if args.phase == "write":
            from plugin_orm import with_session

            @with_session
            async def aborted(_, session):
                await manager.add_subscribe(
                    "single", "rollback", event("direct"), uid="x"
                )
                raise ValueError("aborted")

            try:
                await aborted(None)
            except ValueError:
                pass
            assert not await manager.get_subscribe("rollback")
        else:
            await denied(manager.add_subscribe("single", "denied", event("direct")))
        frames = []

        async def emit(frame):
            frames.append(frame)

        install_subscribe(package, orm, target=BoundTarget("group", "room", emit))
        assert await rows[0].send("fixture") == 0
        await denied(rows[0].send("wrong target", force_direct=True))
        assert frames == [[{"type": "text", "data": "fixture"}]]
    for scope in (nullcontext(), binding_scope("actor", "adapter", False)):
        with scope:
            await denied(manager.get_subscribe("test"))
    with binding_scope("other", "adapter", True):
        assert not await manager.get_subscribe("test")
    return {"observations": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("loader", "native"))
    parser.add_argument("--phase", choices=("write", "reopen"))
    parser.add_argument(
        "--reference", type=Path, default=ROOT / ".sayu-local/reference"
    )
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.mode:
        result = asyncio.run(child(args))
    else:
        profile = json.loads((ROOT / "profiles/lock.json").read_bytes())["starrail"]
        exe, _ = selected(
            ROOT / ".sayu-local/starrail",
            (ROOT / "profiles/starrail-requirements.txt").read_bytes(),
        )
        with tempfile.TemporaryDirectory(prefix="sayu-subscribe-") as folder:
            root = Path(folder)
            archive = root / "worker.pyz"
            archive.write_bytes(worker_archive(profile))
            result = {"reference": REFERENCE_COMMIT}
            for kind, python in (("native", sys.executable), ("loader", exe)):
                work = root / kind
                work.mkdir()
                if kind == "native":
                    copy_reference(args.reference.resolve(), work)
                phases = []
                for phase in ("write", "reopen"):
                    out = work / (phase + ".json")
                    subprocess.run(
                        [
                            str(python),
                            "-IB",
                            "-X",
                            "utf8",
                            __file__,
                            "--mode",
                            kind,
                            "--phase",
                            phase,
                            "--archive",
                            str(archive),
                            "--output",
                            str(out),
                        ],
                        cwd=work,
                        check=True,
                        timeout=120,
                    )
                    phases.append(json.loads(out.read_bytes())["observations"])
                result[kind] = phases
            if (
                result["loader"] != result["native"]
                or result["loader"][0] != result["loader"][1]
            ):
                raise AssertionError("subscription observations or recovery differ")
    args.output.write_text(
        json.dumps(result, ensure_ascii=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
