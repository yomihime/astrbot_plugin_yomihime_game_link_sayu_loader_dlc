"""Compare the fixed account cache against real isolated SQLite databases."""

import argparse
import asyncio
from contextlib import nullcontext
from contextvars import copy_context
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_local  # noqa: E402
from native_contract import REFERENCE_COMMIT, copy_reference, offline  # noqa: E402


async def exercise(mode, archive):
    sys.addaudithook(offline)
    work = Path.cwd()
    if mode == "native":
        sys.path.insert(0, str(work))
        from gsuid_core.utils.database import base_models

        await base_models.init_database()
        from gsuid_core.utils.database import startup

        await startup.create_core_tables()

        def scope(*args, **kwargs):
            return nullcontext()
    else:
        sys.path.insert(0, str(archive))
        from plugin_bindings import binding_scope
        from port_errors import Unsupported
        from plugin_runtime import PluginRuntime
        from sayu_compat import install
        from service_settings import management_scope

        data = work / "private"
        source = work / "plugins"
        source.mkdir()
        install("Fixture", "gs", data, source_root=source)
        runtime = PluginRuntime(data)
        runtime.install(source, "Fixture")
        orm = runtime.orm
        with management_scope(writable=True):
            async with orm.startup():
                await orm.migrate()

        def scope(actor="one", adapter="fixture", private=True, account=True):
            return binding_scope(actor, adapter, private, account_records=account)

    from gsuid_core.utils.database.models import GsCache, GsUser

    uid, sr = "100000007", "100000017"
    first, second = "synthetic-first", "synthetic-second"
    with scope():
        for value in (first, second):
            await GsUser.full_insert_data(
                user_id="one",
                bot_id="fixture",
                uid=uid,
                sr_uid=sr,
                cookie=value,
            )
        assert await GsCache.select_cache_cookie(uid, None) is None
        assert await GsCache.insert_cache_data(first, uid=uid)
        assert await GsCache.insert_cache_data(first, sr_uid=sr)
        assert await GsCache.select_cache_cookie(uid, "gs") == first
        assert await GsCache.select_cache_cookie(sr, "sr") == first
        assert await GsCache.refresh_cache(uid)
        assert await GsCache.select_cache_cookie(uid, "gs") is None
        assert await GsCache.select_cache_cookie(sr, "sr") == first

    result = {"matching_uid_sr_refresh": True}
    if mode == "native":
        from sqlalchemy import text

        async with base_models.engine.begin() as conn:
            await conn.execute(
                text("CREATE UNIQUE INDEX test_cache_sr ON gscache(sr_uid)")
            )
        db_path = None
    else:
        identity = hashlib.sha256(json.dumps(["fixture", "one"]).encode()).hexdigest()
        db_path = orm.root / (identity + ".sqlite3")
        with sqlite3.connect(db_path) as db:
            db.execute("CREATE UNIQUE INDEX test_cache_sr ON gscache(sr_uid)")

    from sqlalchemy.exc import IntegrityError

    with scope():
        try:
            await GsCache.insert_cache_data(second, sr_uid=sr)
        except IntegrityError:
            pass
        else:
            raise AssertionError("expected real uniqueness failure")
        assert await GsCache.select_cache_cookie(sr, "sr") == first
        assert await GsCache.insert_cache_data(second, uid=uid)
        assert await GsCache.select_cache_cookie(uid, None) == second
    result["constraint_rollback_and_reopen"] = True
    if mode == "native":
        async with base_models.engine.begin() as conn:
            await conn.execute(
                text("UPDATE gsuser SET status='invalid' WHERE cookie=:c"), {"c": first}
            )
        assert await GsCache.delete_error_cache(GsUser)
        # get_all_error_cookie calls get_all_user(without_error=True), whose
        # default query discards the error row before filtering for errors.
        assert await GsUser.get_all_error_cookie() == []
        assert await GsCache.select_cache_cookie(sr, "sr") == first
        assert await GsCache.select_cache_cookie(uid, None) == second
        async with base_models.engine.begin() as conn:
            await conn.execute(
                text("UPDATE gsuser SET status='limit30' WHERE cookie=:c"),
                {"c": second},
            )
        assert await GsCache.delete_all_cache(GsUser)
        assert await GsCache.select_cache_cookie(uid, None) is None
        async with base_models.engine.begin() as conn:
            statuses = dict(
                (await conn.execute(text("SELECT cookie,status FROM gsuser"))).all()
            )
        assert statuses == {first: "invalid", second: None}
        return {
            **result,
            "error_cleanup_returns_true_but_retains_invalid": True,
            "global_cleanup_clears_limit30": True,
        }

    async def denied(coro):
        try:
            await coro
        except Unsupported:
            return
        raise AssertionError("unexpected cache permission")

    from sqlalchemy import text

    with sqlite3.connect(db_path) as db:
        db.executemany(
            "UPDATE gsuser SET status=? WHERE cookie=?",
            [("invalid", first), ("limit30", second)],
        )
    for name, adapter in (("two", "fixture"), ("one", "other")):
        with scope(name, adapter):
            assert await GsCache.select_cache_cookie(uid, None) is None
            await denied(GsCache.insert_cache_data(second, uid=uid))
    for private, account in ((True, False), (False, True)):
        with scope(private=private, account=account):
            await denied(GsCache.select_cache_cookie(uid, None))
            await denied(GsCache.refresh_cache(uid))
    with scope():
        await denied(GsCache.insert_cache_data("unassociated", uid=uid))
        await denied(GsCache.insert_cache_data(first, uid="999"))
        await denied(GsCache.insert_cache_data(first, uid=uid, id=123))
        await denied(GsCache.full_insert_data(cookie=first, uid=uid))
        await denied(GsCache.delete_error_cache(GsUser))
        await denied(GsCache.delete_all_cache(GsUser))
        assert await GsCache.select_cache_cookie(uid, None) == second
        assert await GsCache.select_cache_cookie(sr, "sr") == first
        await denied(asyncio.create_task(GsCache.select_cache_cookie(uid, None)))

        # This private profile engine grants no raw account table access.
        async def raw():
            async with orm.connect() as conn:
                await conn.execute(text("SELECT cookie FROM gscache"))

        await denied(raw())
        from gsuid_core.utils.database.base_models import with_session

        @with_session
        async def insert_object(cls, session):
            session.add(GsCache(cookie="unassociated", uid=uid))

        await denied(insert_object(GsCache))
        expired = copy_context()
    with binding_scope("one", "fixture", True, writable=True):
        await denied(insert_object(None))
        await denied(GsCache.refresh_cache(uid))
    await denied(
        expired.run(asyncio.create_task, GsCache.select_cache_cookie(uid, None))
    )
    await denied(GsCache.select_cache_cookie(uid, None))
    with sqlite3.connect(db_path) as db:
        assert dict(db.execute("SELECT cookie,status FROM gsuser")) == {
            first: "invalid",
            second: "limit30",
        }
        db.execute("INSERT INTO gscache(uid,cookie) VALUES(?,?)", ("999", first))
        db.execute(
            "UPDATE gsuser SET user_id=?, uid=? WHERE cookie=?", ("two", "998", second)
        )
        db.execute("INSERT INTO gscache(uid,cookie) VALUES(?,?)", ("998", second))
    with scope():
        await denied(GsCache.select_cache_cookie("998", None))
        await denied(GsCache.insert_cache_data(second, uid="998"))
        await denied(GsCache.select_cache_cookie("999", None))
        assert await GsCache.refresh_cache("999")
        assert await GsCache.select_cache_cookie("999", None) is None
    result["owner_adapter_account_and_maintenance_grants"] = True
    result["denied_misfiled_account_and_orphan_read_owner_can_evict"] = True
    result["denied_constructor_stale_scope_and_foreign_task"] = True
    # Simulate an older existing owner file with user rows but no cache table.
    with sqlite3.connect(db_path) as db:
        db.execute("DROP TABLE gscache")
    with management_scope(writable=True):
        async with orm.startup():
            await orm.migrate()
    with scope():
        assert await GsCache.select_cache_cookie(uid, None) is None
        assert await GsCache.insert_cache_data(first, uid=uid)
        assert await GsCache.select_cache_cookie(uid, None) == first
    result["existing_owner_migrated_and_user_preserved"] = True
    with sqlite3.connect(db_path) as db:
        db.execute("ALTER TABLE gscache ADD COLUMN legacy TEXT")
    with scope():
        await denied(GsCache.select_cache_cookie(uid, None))
    result["unknown_existing_schema_fails_closed"] = True
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reference", type=Path, default=build_local.LOCAL / "reference"
    )
    parser.add_argument("--loader-python", type=Path, default=Path(sys.executable))
    parser.add_argument("--child", choices=("native", "loader"))
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    opts = parser.parse_args()
    if opts.child:
        report = asyncio.run(exercise(opts.child, opts.worker))
    else:
        reference = opts.reference.resolve()
        with tempfile.TemporaryDirectory() as dirname:
            temp = Path(dirname)
            sources = copy_reference(reference, temp / "native")
            (temp / "loader").mkdir()
            build_local.LOCAL = reference.parent
            spec = json.loads((build_local.ROOT / "profiles/lock.json").read_bytes())[
                "genshin"
            ]
            archive = temp / "worker.pyz"
            archive.write_bytes(build_local.worker_archive(spec, offline=True))
            report = {
                "reference_commit": REFERENCE_COMMIT,
                "reference_files": sources,
                "worker_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            }
            for mode, exe in (
                ("native", sys.executable),
                ("loader", opts.loader_python.resolve()),
            ):
                out = temp / (mode + ".json")
                subprocess.run(
                    [
                        str(exe),
                        "-IB",
                        "-X",
                        "utf8",
                        str(Path(__file__).resolve()),
                        "--child",
                        mode,
                        "--worker",
                        str(archive),
                        "--output",
                        str(out),
                    ],
                    check=True,
                    cwd=temp / mode,
                    timeout=120,
                )
                report[mode] = json.loads(out.read_bytes())
    opts.output.write_text(json.dumps(report, indent=2), encoding="utf8")


if __name__ == "__main__":
    main()
