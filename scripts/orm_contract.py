"""Run unmodified Waves database models against native GsCore and the packaged SQLite port."""

import argparse
import asyncio
from contextlib import nullcontext
import hashlib
import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build_local import worker_archive  # noqa: E402
from native_contract import REFERENCE_COMMIT, copy_reference, offline  # noqa: E402

PLUGIN = "XutheringWavesUID"


def load_models(source):
    # The subject is the complete database subpackage, including its real imports,
    # admin declarations and migration hook, isolated from unrelated game startup.
    outer = "plugins." + PLUGIN
    name = outer + "." + PLUGIN
    inner = PLUGIN + "/" + PLUGIN
    for path, relative in (
        ("plugins", ""),
        (outer, PLUGIN),
        (name, inner),
        (name + ".utils", inner + "/utils"),
    ):
        module = ModuleType(path)
        module.__path__ = [str(source / relative)]
        sys.modules[path] = module
    return importlib.import_module(name + ".utils.database.models")


async def business(models, base):
    from sqlalchemy import text

    result = []
    bind = models.WavesBind
    for uid in ("", "not-a-number", "1102", "1102", "1205"):
        result.append(await bind.insert_waves_uid("actor", "adapter", uid, "group"))
    result.append(await bind.get_uid_list_by_game("actor", "adapter"))
    result.append(await bind.switch_uid_by_game("actor", "adapter"))
    result.append(await bind.get_uid_by_game("actor", "adapter"))
    result.append([row.uid for row in await bind.get_binds_by_uid("1102")])
    result.append(
        await models.WavesUserActivity.update_user_activity("actor", "adapter", "robot")
    )
    result.append(
        await models.WavesUser.insert_data(
            "actor", "adapter", uid="1102", cookie="fixture"
        )
    )
    result.append(await models.WavesUser.get_user_cookie_by_uid("1102"))

    @base.with_session
    async def failed(cls, session):
        await session.execute(text("UPDATE WavesBind SET uid='uncommitted'"))
        raise ValueError("force rollback")

    @base.with_session
    async def nested(cls, session):
        try:
            await failed(cls)
        except ValueError:
            return await bind.get_uid_by_game("actor", "adapter")

    result.append(await nested(bind))
    try:
        await failed(bind)
    except ValueError:
        pass
    result.append(await bind.get_uid_by_game("actor", "adapter"))
    return result


async def child(kind, source, worker):
    sys.addaudithook(offline)
    work = Path.cwd()
    if kind == "native":
        sys.path.insert(0, str(work))
        from gsuid_core.utils.database import base_models as base

        await base.init_database()
        from gsuid_core.utils.database import startup

        models = load_models(source)
        await startup.create_core_tables()
        await startup.trans_adapter()
        await models._waves_auto_migrate()
        return {"observations": await business(models, base)}
    sys.path.insert(0, str(worker))
    import plugin_bindings
    from port_errors import Unsupported
    from plugin_runtime import PluginRuntime
    from sayu_compat import install
    from service_settings import management_scope
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    data = work / "private"
    install(PLUGIN, "ww", data, source_root=source)
    runtime = PluginRuntime(data)
    runtime.install(source, PLUGIN)
    orm = runtime.orm
    with management_scope(writable=True):
        async with orm.startup():
            models = load_models(source)
            first = await orm.migrate()
            await models._waves_auto_migrate()
            if kind == "loader":
                assert (
                    await models.WavesBind.insert_waves_uid(
                        "startup-seed", "adapter", "9393"
                    )
                    == 0
                )
    from gsuid_core.utils.database import base_models as base

    def actor(name="actor", *, private=True, writable=False):
        return plugin_bindings.binding_scope(
            name, "adapter", private, writable=writable
        )

    if kind == "reload":
        with actor():
            uid = await models.WavesBind.get_uid_by_game("actor", "adapter")
            assert (
                uid == "1205"
                and first["already_applied"] == 48
                and first["applied"] == 0
            )
        return {"persistent_uid": uid, "migration": first}
    with actor(writable=True):
        assert await models.WavesBind.get_binds_by_uid("9393") == []
        observed = await business(models, base)

    @base.with_session
    async def increment(cls, session):
        before = (
            await session.execute(
                text("SELECT last_active_time FROM WavesUserActivity")
            )
        ).scalar_one()
        await asyncio.sleep(0.01)
        await session.execute(
            text("UPDATE WavesUserActivity SET last_active_time=:t"), {"t": before + 1}
        )
        return before

    async def independent_write():
        with actor(writable=True):
            return await increment(None)

    one, two = sorted(await asyncio.gather(independent_write(), independent_write()))
    assert two == one + 1

    async def must_deny(op):
        try:
            await op
        except Unsupported:
            return True
        raise AssertionError("operation unexpectedly succeeded")

    limits = []
    with actor():
        assert await models.WavesBind.get_uid_by_game("actor", "adapter") == "1205"

        @base.with_session
        async def raw(cls, session):
            await session.execute(text("UPDATE WavesBind SET uid='forbidden'"))

        limits.append(await must_deny(raw(None)))
        limits.append(await must_deny(models.WavesBind.select_data("other", "adapter")))
        limits.append(
            await must_deny(
                asyncio.create_task(models.WavesBind.select_data("actor", "adapter"))
            )
        )
    for scope in (nullcontext(), actor(private=False)):
        with scope:
            limits.append(
                await must_deny(models.WavesBind.select_data("actor", "adapter"))
            )
    with actor("other"):
        assert await models.WavesBind.get_binds_by_uid("1205") == []
        assert await models.WavesBind.get_binds_by_uid("9393") == []
    with management_scope(writable=True):
        async with orm.startup():
            repeated = await orm.migrate()
            orm.migrations.extend(
                ["CREATE TABLE should_rollback (id INTEGER)", "INVALID SQL"]
            )
            try:
                await orm.migrate()
            except DBAPIError:
                assert orm.report["failed"] == 1
                failed = dict(orm.report)
            else:
                raise AssertionError("broken migration was accepted")
            async with orm.connect() as conn:
                assert not (
                    await conn.exec_driver_sql(
                        "SELECT name FROM sqlite_master WHERE name='should_rollback'"
                    )
                ).first()
            orm.migrations.pop()
            recovered = await orm.migrate()
    assert repeated["applied"] == 0 and repeated["files"] == 2
    with actor():
        assert await models.WavesBind.get_uid_by_game("actor", "adapter") == "1205"
    return {
        "observations": observed,
        "owner_checks": len(limits),
        "concurrent_increments": 2,
        "migrations": [first, repeated, failed, recovered],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", choices=("loader", "native", "reload"))
    parser.add_argument("--source", type=Path)
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--loader-python", type=Path, default=Path(sys.executable))
    parser.add_argument(
        "--reference", type=Path, default=ROOT / ".sayu-local/reference"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.child:
        report = asyncio.run(child(args.child, args.source, args.worker))
    else:
        lock = json.loads((ROOT / "profiles/lock.json").read_bytes())["waves"]
        digest = lock["snapshot_sha256"]
        snap = ROOT / f".sayu-local/runtime/{PLUGIN}-{digest}.zip"
        assert hashlib.sha256(snap.read_bytes()).hexdigest() == digest
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            source, native, loader = (work / p for p in ("source", "native", "loader"))
            sources = copy_reference(args.reference.resolve(), native)
            loader.mkdir()
            with ZipFile(snap) as zipped:
                prefix = f"{PLUGIN}/{PLUGIN}/utils/database/"
                for name in zipped.namelist():
                    if name.startswith(prefix):
                        zipped.extract(name, source)
            archive = work / "worker.pyz"
            archive.write_bytes(worker_archive(lock))
            runs = {}
            for kind, interpreter in (
                ("loader", args.loader_python.resolve()),
                ("reload", args.loader_python.resolve()),
                ("native", sys.executable),
            ):
                output = work / (kind + ".json")
                subprocess.run(
                    [
                        str(interpreter),
                        "-IB",
                        "-X",
                        "utf8",
                        __file__,
                        "--child",
                        kind,
                        "--source",
                        str(source),
                        "--worker",
                        str(archive),
                        "--output",
                        str(output),
                    ],
                    cwd=loader if kind == "reload" else work / kind,
                    check=True,
                    timeout=120,
                )
                runs[kind] = json.loads(output.read_bytes())
            report = {
                "reference": REFERENCE_COMMIT,
                "reference_files": sources,
                "source_sha256": digest,
                "worker_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                **runs,
            }
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if (
        not args.child
        and report["native"]["observations"] != report["loader"]["observations"]
    ):
        raise SystemExit("native/loader model results differ")


if __name__ == "__main__":
    main()
