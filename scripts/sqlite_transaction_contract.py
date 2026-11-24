"""Run a synthetic transaction workload against pinned GsCore and a frozen worker."""

import argparse
import asyncio
from contextlib import nullcontext
import hashlib
from importlib.metadata import version
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from shutil import copytree

sys.path.insert(0, str(Path(__file__).resolve().parent))
from environments import selected
from native_contract import ROOT, REFERENCE_COMMIT, copy_reference, offline

LOCAL = ROOT / ".sayu-local"
SCHEMA = "CREATE TABLE IF NOT EXISTS transaction_sample (label TEXT PRIMARY KEY, amount INTEGER NOT NULL)"


class FixtureFailure(Exception):
    pass


def watch_transactions():
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    sink = {}

    def capture(sql):
        target = sink.get("capture")
        if target is None:
            return
        operation = sql.split(None, 1)[0].upper()
        if operation in {"BEGIN", "SAVEPOINT", "RELEASE", "ROLLBACK", "COMMIT"}:
            target.append(sql.strip().upper())
        elif operation in {"INSERT", "UPDATE", "DELETE"}:
            target.append(operation)

    @event.listens_for(Engine, "connect")
    def install(connection, record):
        if hasattr(connection, "run_async"):
            connection.run_async(lambda driver: driver.set_trace_callback(capture))

    return sink


def driver_control(directory):
    """Isolate SQLite driver effects from SQLAlchemy, GsCore, and the loader."""
    from contextlib import closing

    result = {}
    for explicit in (False, True):
        path = directory / f"driver-{int(explicit)}.sqlite3"
        with closing(sqlite3.connect(path)) as a, closing(sqlite3.connect(path)) as b:
            a.execute("PRAGMA journal_mode=WAL")
            a.execute("CREATE TABLE fixture (n INTEGER)")
            a.execute("INSERT INTO fixture VALUES (0)")
            a.commit()
            observation = {
                "autocommit": a.autocommit,
                "isolation_level": a.isolation_level,
            }
            if explicit:
                a.execute("BEGIN")
            a.execute("SAVEPOINT nested")
            a.execute("INSERT INTO fixture VALUES (1)")
            a.execute("RELEASE SAVEPOINT nested")
            observation["outer_transaction_after_release"] = a.in_transaction
            a.rollback()
            observation["first_savepoint_survived"] = bool(
                a.execute("SELECT COUNT(*) FROM fixture WHERE n=1").fetchone()[0]
            )
            if explicit:
                a.execute("BEGIN")
            a.execute("CREATE TABLE transaction_ddl (n INTEGER)")
            a.rollback()
            observation["ddl_survived_rollback"] = bool(
                a.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='transaction_ddl'"
                ).fetchone()
            )
            if explicit:
                a.execute("BEGIN")
            before = a.execute("SELECT COUNT(*) FROM fixture").fetchone()[0]
            b.execute("INSERT INTO fixture VALUES (2)")
            b.commit()
            after = a.execute("SELECT COUNT(*) FROM fixture").fetchone()[0]
            observation["repeat_read_saw_intervening_commit"] = after != before
            a.rollback()
            result["explicit_begin" if explicit else "driver_default"] = observation
    return result


async def workload(base, owner, phase, trace):
    from sqlalchemy import text

    @base.with_session
    async def write(cls, session, function):
        return await function(session)

    @base.with_read_session
    async def read(cls, session, function):
        return await function(session)

    async def put(session, label, amount=0):
        await session.execute(
            text(
                "INSERT INTO transaction_sample(label, amount) VALUES (:label,:amount)"
            ),
            {"label": label, "amount": amount},
        )

    async def state(session):
        result = await session.execute(
            text("SELECT label, amount FROM transaction_sample ORDER BY label")
        )
        return dict(result.all())

    async def view(actor, adapter="adapter"):
        with owner(actor, adapter, False):
            return await read(None, state)

    if phase == "reopen":
        return {
            key: await view(*identity)
            for key, identity in {
                "a": ("first", "adapter"),
                "b": ("second", "adapter"),
                "other_adapter": ("first", "other"),
            }.items()
        }

    facts = {}

    async def operation(label, action):
        statements = []
        with owner("first", "adapter", True):
            trace["capture"] = statements
            try:
                await write(None, action)
                status = "completed"
            except Exception as exc:
                status = type(exc).__name__
            finally:
                trace.pop("capture")
        facts[label] = {"outcome": status, "after": await view("first")}
        facts[label]["sqlite_trace"] = statements

    async def rollback(session):
        await put(session, "rolled-back")
        await session.rollback()
        await put(session, "after-rollback")

    async def exception(session):
        await put(session, "exception")
        raise FixtureFailure()

    async def explicit_commit(session):
        await put(session, "explicit-commit")
        await session.commit()
        await put(session, "after-explicit-commit")
        raise FixtureFailure()

    async def duplicate(session):
        await put(session, "prior-to-duplicate")
        await put(session, "implicit-commit")

    async def nested_recover(session):
        await put(session, "outer-before")
        try:
            await write(None, exception)
        except FixtureFailure:
            pass
        facts["nested_read_sees_outer"] = "outer-before" in await read(None, state)
        await put(session, "outer-after")

    async def outer_abort(session):
        await put(session, "outer-abort")
        await write(None, lambda s: put(s, "released-savepoint"))
        raise FixtureFailure()

    async def nested_first_abort(session):
        # No preceding DML in the outer session; SQLite's transaction start
        # rules can change whether releasing this first savepoint commits it.
        await write(None, lambda s: put(s, "first-savepoint"))
        raise FixtureFailure()

    await operation("implicit_commit", lambda s: put(s, "implicit-commit"))
    await operation("explicit_rollback", rollback)
    await operation("application_exception", exception)
    await operation("commit_then_exception", explicit_commit)
    await operation("constraint_exception", duplicate)
    await operation("nested_recovery", nested_recover)
    await operation("outer_rollback", outer_abort)
    await operation("first_savepoint_outer_rollback", nested_first_abort)

    ready, release = asyncio.Event(), asyncio.Event()

    async def hold(session):
        await put(session, "concurrent-commit")
        ready.set()
        await release.wait()

    async def write_in_scope(action):
        with owner("first", "adapter", True):
            return await write(None, action)

    pending = asyncio.create_task(write_in_scope(hold))
    try:
        await asyncio.wait_for(ready.wait(), 10)
        reader = asyncio.create_task(view("first"))
        done, _ = await asyncio.wait({reader}, timeout=1)
        facts["read_while_uncommitted"] = {"completed_before_release": bool(done)}
    finally:
        release.set()
        await pending
    facts["read_while_uncommitted"]["saw_pending_value"] = (
        "concurrent-commit" in await reader
    )

    await write_in_scope(lambda s: put(s, "counter"))
    first_read, second_called, unlock = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )

    async def add_one(session, pause=False):
        value = (await state(session))["counter"]
        if pause:
            first_read.set()
            await unlock.wait()
        await session.execute(
            text("UPDATE transaction_sample SET amount=:new WHERE label='counter'"),
            {"new": value + 1},
        )
        return value

    async def second_write():
        second_called.set()
        return await write_in_scope(add_one)

    a = asyncio.create_task(write_in_scope(lambda s: add_one(s, True)))
    try:
        await asyncio.wait_for(first_read.wait(), 10)
        b = asyncio.create_task(second_write())
        await asyncio.wait_for(second_called.wait(), 10)
    finally:
        unlock.set()
    results = await asyncio.gather(a, b)
    facts["concurrent_writers"] = {
        "observed": results,
        "final": (await view("first"))["counter"],
    }

    inserted, cancel_wait = asyncio.Event(), asyncio.Event()

    async def cancel_me(session):
        await put(session, "cancelled")
        inserted.set()
        await cancel_wait.wait()

    task = asyncio.create_task(write_in_scope(cancel_me))
    await asyncio.wait_for(inserted.wait(), 10)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        facts["cancelled_write"] = {
            "outcome": "CancelledError",
            "persisted": "cancelled" in await view("first"),
        }
    await write_in_scope(lambda s: put(s, "post-cancel"))
    facts["other_owner_before_write"] = await view("second")
    with owner("second", "adapter", True):
        await write(None, lambda s: put(s, "other-owner"))
    facts["before_reopen"] = {
        "a": await view("first"),
        "b": await view("second"),
        "other_adapter": await view("first", "other"),
    }
    return facts


async def child(kind, phase, worker):
    sys.addaudithook(offline)
    work = Path.cwd()
    trace = watch_transactions()
    if kind.startswith("native"):
        sys.path.insert(0, str(work))
        from gsuid_core.utils.database import base_models as base

        await base.init_database()
        if kind == "native_explicit_begin":
            from sqlalchemy import event

            @event.listens_for(base.engine.sync_engine, "connect")
            def disable_driver_begin(connection, record):
                connection.isolation_level = None

            @event.listens_for(base.engine.sync_engine, "begin")
            def start_transaction(connection):
                connection.exec_driver_sql("BEGIN")

        async with base.engine.begin() as connection:
            await connection.exec_driver_sql(SCHEMA)
        try:
            result = await workload(base, lambda *args: nullcontext(), phase, trace)
        finally:
            await base.engine.dispose()
    else:
        sys.path.insert(0, str(worker))
        import sayu_compat
        from plugin_bindings import binding_scope
        from plugin_runtime import PluginRuntime
        from service_settings import management_scope

        sayu_compat.install("TransactionFixture", "tx", work / "data", source_root=work)
        runtime = PluginRuntime(work / "data")
        runtime.install(work, "TransactionFixture")
        with management_scope(writable=True):
            async with runtime.orm.startup():
                runtime.orm.migrations.append(SCHEMA)
                await runtime.orm.migrate()
        from gsuid_core.utils.database import base_models as base

        result = await workload(
            base,
            lambda actor, adapter, writable: binding_scope(
                actor, adapter, True, writable=writable
            ),
            phase,
            trace,
        )
    return {
        "results": result,
        "versions": {
            "python": sys.version.split()[0],
            "sqlite": sqlite3.sqlite_version,
            "sqlalchemy": version("sqlalchemy"),
            "aiosqlite": version("aiosqlite"),
        },
    }


def run(args):
    freeze = json.loads(args.freeze.read_bytes())
    lock = freeze["locked"][args.profile]
    worker = LOCAL / "runtime" / f"{args.profile}-{lock['worker']}.pyz"
    worker_hash = hashlib.sha256(worker.read_bytes()).hexdigest()
    if worker_hash != lock["worker"]:
        raise ValueError("frozen worker digest mismatch")
    requirements = (ROOT / "profiles" / f"{args.profile}-requirements.txt").read_bytes()
    if hashlib.sha256(requirements).hexdigest() != lock["requirements"]:
        raise ValueError("installed profile differs from the frozen requirements")
    loader, _ = selected(LOCAL / args.profile, requirements)
    report = {
        "candidate": freeze["candidate"],
        "worker_sha256": worker_hash,
        "source_sha256": lock["source"],
        "requirements_sha256": lock["requirements"],
        "reference": REFERENCE_COMMIT,
        "runs": {},
    }
    with tempfile.TemporaryDirectory(prefix="sayu-transaction-") as directory:
        tmp = Path(directory)
        reference = tmp / "native"
        report["reference_files"] = copy_reference(args.reference, reference)
        copytree(reference, tmp / "native_explicit_begin")
        report["driver_control"] = driver_control(tmp)
        (tmp / "loader").mkdir()
        for kind, interpreter in (
            ("native", sys.executable),
            ("native_explicit_begin", sys.executable),
            ("loader", loader),
        ):
            work = tmp / kind
            home = work / "private-home"
            home.mkdir()
            env = {
                key: value
                for key, value in os.environ.items()
                if key.upper() in {"SYSTEMROOT", "WINDIR", "LANG"}
            }
            env.update(
                {
                    key: str(home)
                    for key in (
                        "HOME",
                        "USERPROFILE",
                        "APPDATA",
                        "LOCALAPPDATA",
                        "TMP",
                        "TEMP",
                        "TMPDIR",
                        "XDG_CACHE_HOME",
                    )
                }
            )
            for phase in ("transaction", "reopen"):
                receipt = tmp / f"{kind}-{phase}.json"
                subprocess.run(
                    [
                        str(interpreter),
                        "-IB",
                        "-X",
                        "utf8",
                        __file__,
                        "--child",
                        kind,
                        "--phase",
                        phase,
                        "--worker",
                        str(worker),
                        "--output",
                        str(receipt),
                    ],
                    cwd=work,
                    env=env,
                    check=True,
                    timeout=90,
                )
                report["runs"].setdefault(kind, {})[phase] = json.loads(
                    receipt.read_bytes()
                )
    original = report["runs"]["native"]["transaction"]["results"]
    candidate = report["runs"]["loader"]["transaction"]["results"]

    def results_only(value):
        return (
            {k: v for k, v in value.items() if k != "sqlite_trace"}
            if isinstance(value, dict)
            else value
        )

    report["differences"] = sorted(
        k
        for k in original.keys() | candidate.keys()
        if results_only(original.get(k)) != results_only(candidate.get(k))
    )
    for key, details in report["runs"].items():
        details["persistent_after_process_restart"] = (
            details["transaction"]["results"]["before_reopen"]
            == details["reopen"]["results"]
        )
    report["checks"] = verify(report)
    return report


def verify(report):
    checks = {}
    for key, run in report["runs"].items():
        rows = run["transaction"]["results"]
        checks[key] = {
            "commit": rows["implicit_commit"]["after"] == {"implicit-commit": 0},
            "rollback_and_continue": set(rows["explicit_rollback"]["after"])
            == {"implicit-commit", "after-rollback"},
            "raised_error_rolled_back": rows["application_exception"]["outcome"]
            == "FixtureFailure"
            and "exception" not in rows["application_exception"]["after"],
            "explicit_commit_survived_later_failure": "explicit-commit"
            in rows["commit_then_exception"]["after"]
            and "after-explicit-commit" not in rows["commit_then_exception"]["after"],
            "constraint_failure_rolled_back": rows["constraint_exception"]["outcome"]
            == "IntegrityError"
            and "prior-to-duplicate" not in rows["constraint_exception"]["after"],
            "nested_error_isolated": rows["nested_read_sees_outer"]
            and {"outer-before", "outer-after"}.issubset(
                rows["nested_recovery"]["after"]
            )
            and "exception" not in rows["nested_recovery"]["after"],
            "outer_dml_then_nested_commit_rolled_back": {
                "outer-abort",
                "released-savepoint",
            }.isdisjoint(rows["outer_rollback"]["after"]),
            "first_savepoint_result": (
                "first-savepoint" in rows["first_savepoint_outer_rollback"]["after"]
            )
            == (key == "native"),
            "concurrent_read": rows["read_while_uncommitted"]
            == {"completed_before_release": True, "saw_pending_value": False},
            "serialized_increment": rows["concurrent_writers"]
            == {"observed": [0, 1], "final": 2},
            "cancel_and_recover": rows["cancelled_write"]
            == {"outcome": "CancelledError", "persisted": False}
            and "post-cancel" in rows["before_reopen"]["a"],
            "independent_process_reopen": run["persistent_after_process_restart"],
        }
        visible = run["reopen"]["results"]
        checks[key]["owner_visibility"] = (
            visible["a"] == visible["b"] == visible["other_adapter"]
            if key != "loader"
            else visible["b"] == {"other-owner": 0}
            and visible["other_adapter"] == {}
            and "other-owner" not in visible["a"]
        )
    return checks


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--child", choices=("native", "native_explicit_begin", "loader")
    )
    parser.add_argument("--phase", choices=("transaction", "reopen"))
    parser.add_argument(
        "--profile", default="starrail", choices=("starrail", "genshin", "waves")
    )
    parser.add_argument("--freeze", type=Path, default=LOCAL / "freeze-8.json")
    parser.add_argument("--reference", type=Path, default=LOCAL / "reference")
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.child:
        try:
            report = asyncio.run(child(args.child, args.phase, args.worker))
        finally:
            logging.shutdown()
            sys.excepthook = sys.__excepthook__
    else:
        report = run(args)
    args.output.write_text(json.dumps(report, indent=2), "utf8")
    if not args.child:
        print("Observed differences:", sorted(report["differences"]))
        if not all(all(checks.values()) for checks in report["checks"].values()):
            raise SystemExit(
                "A transaction observation changed. See the saved receipt."
            )
