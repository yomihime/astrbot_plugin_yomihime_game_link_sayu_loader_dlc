"""Compare the public thread decorators against the pinned native pool."""

import argparse
import asyncio
from contextvars import ContextVar, copy_context
import hashlib
import inspect
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build_local import worker_archive  # noqa: E402
from native_contract import REFERENCE_COMMIT, copy_reference, offline  # noqa: E402


async def compare(pool):
    calling_loop = asyncio.get_running_loop()
    caller = threading.get_ident()
    context = ContextVar("test_pool_value", default="default")
    context.set("caller")

    def work(value, *, delta):
        previous = context.get()
        context.set("worker")
        return [value + delta, threading.get_ident() != caller, previous]

    decorated = pool.to_thread(work)
    first = await decorated(4, delta=3)
    second = await decorated(8, delta=2)

    @pool.run_in_thread_pool
    async def coro(number):
        await asyncio.sleep(0)
        return [
            number,
            threading.get_ident() != caller,
            asyncio.get_running_loop() is not calling_loop,
            context.get(),
        ]

    @pool.to_thread
    def returns_awaitable():
        async def later():
            return threading.get_ident() == caller

        return later()

    @pool.to_thread
    async def broken_async():
        raise ValueError("async failure")

    @pool.to_thread
    def broken():
        raise ValueError("sync failure")

    failures = []
    for func in (broken, broken_async):
        try:
            await func()
        except ValueError as exc:
            failures.append(str(exc))
    pending = await returns_awaitable()
    assert inspect.iscoroutine(pending)
    result = {
        "alias": pool.to_thread is pool.run_in_thread_pool,
        "metadata": decorated.__name__ == "work" and decorated.__wrapped__ is work,
        "sync": [first, second],
        "async": await coro(11),
        "caller_context_after": context.get(),
        "returned_awaitable_on_caller": await pending,
        "errors": failures,
    }
    assert result == {
        "alias": True,
        "metadata": True,
        "sync": [[7, True, "default"], [10, True, "worker"]],
        "async": [11, True, True, "worker"],
        "caller_context_after": "caller",
        "returned_awaitable_on_caller": True,
        "errors": ["sync failure", "async failure"],
    }

    ready, release = threading.Event(), threading.Event()
    entered = set()
    lock = threading.Lock()

    @pool.to_thread
    def block(i):
        with lock:
            entered.add(threading.get_ident())
            if len(entered) == 10:
                ready.set()
        if not release.wait(8):
            raise TimeoutError("test gate never released")
        return i

    running = [asyncio.create_task(block(i)) for i in range(10)]
    try:
        assert await asyncio.to_thread(ready.wait, 5)
        queued = asyncio.create_task(block(11))
        await asyncio.sleep(0)
        pool.shutdown_pools()
        pool.shutdown_pools()
        try:
            await asyncio.wait_for(queued, 2)
        except asyncio.CancelledError:
            result["cancelled_queued"] = True
        try:
            await decorated(1, delta=0)
        except RuntimeError as exc:
            result["after_shutdown"] = type(exc).__name__
    finally:
        release.set()
    assert await asyncio.gather(*running) == list(range(10))
    result["finished_running"] = len(entered)
    assert result["cancelled_queued"] and result["after_shutdown"] == "RuntimeError"
    return result


async def check_authority(pool):
    from plugin_bindings import GsBind, binding_scope
    from port_errors import Unsupported
    from service_settings import ServiceSettings, management_scope

    settings = ServiceSettings(Path.cwd())
    owner = SimpleNamespace(name="probe")
    marker = threading.Event()
    release = threading.Event()

    def probe():
        denied = []
        try:
            settings.update("service", owner, {"enabled": True})
        except NotImplementedError:
            denied.append("management")
        try:
            with GsBind.transaction("actor", "astrbot", write=True):
                pass
        except NotImplementedError:
            denied.append("binding")
        return denied

    with (
        management_scope(writable=True),
        binding_scope("actor", "astrbot", True, writable=True),
    ):
        assert await pool.to_thread(probe)() == ["management", "binding"]
        captured = copy_context()

        @pool.to_thread
        def late():
            marker.set()
            if not release.wait(5):
                raise TimeoutError("test gate never released")
            return captured.run(probe)

        running = asyncio.create_task(late())
        assert await asyncio.to_thread(marker.wait, 3)
    release.set()
    assert await running == ["management", "binding"]
    assert not settings.path.exists()
    try:
        await pool.to_process(lambda: True)()
    except Unsupported as exc:
        rejection = str(exc)
    else:
        raise AssertionError("process worker was allowed")
    return {
        "private_scope_in_thread": "denied",
        "revoked_copy_in_thread": "denied",
        "process": rejection,
    }


async def exercise(mode, worker):
    fallback = getattr(getattr(socket, "_fallback_socketpair", None), "__code__", None)

    def isolated(event, args):
        # On Windows each ProactorEventLoop uses a self-connected loopback pair.
        if (
            event in ("socket.bind", "socket.connect")
            and sys._getframe(1).f_code is fallback
            and args[1][0] in ("127.0.0.1", "::1")
        ):
            return
        offline(event, args)

    sys.addaudithook(isolated)
    if mode == "native":
        sys.path.insert(0, str(Path.cwd()))
        from gsuid_core import pool
    else:
        sys.path.insert(0, str(worker))
        from plugin_pool import install_pool
        from sayu_compat import package

        pool = install_pool(package)
    try:
        report = {"contract": await compare(pool)}
        if mode == "loader":
            fresh = install_pool(package)
            try:
                report["authority"] = await check_authority(fresh)
            finally:
                fresh.shutdown_pools()
            try:
                await pool.to_thread(str)(1)
            except RuntimeError:
                report["old_generation_closed"] = True
            assert report["old_generation_closed"]
        return report
    finally:
        pool.shutdown_pools()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loader-python", type=Path, default=Path(sys.executable))
    parser.add_argument(
        "--reference", type=Path, default=ROOT / ".sayu-local/reference"
    )
    parser.add_argument("--child", choices=("loader", "native"))
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.child:
        report = asyncio.run(exercise(args.child, args.worker))
    else:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            provenance = copy_reference(args.reference.resolve(), work / "native")
            (work / "loader").mkdir()
            worker = work / "worker.pyz"
            worker.write_bytes(
                worker_archive(
                    json.loads((ROOT / "profiles/lock.json").read_bytes())["waves"]
                )
            )
            report = {
                "reference": REFERENCE_COMMIT,
                "sources": provenance,
                "worker_sha256": hashlib.sha256(worker.read_bytes()).hexdigest(),
            }
            for mode, exe in (
                ("native", sys.executable),
                ("loader", args.loader_python.resolve()),
            ):
                output = work / (mode + ".json")
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
                        "--output",
                        str(output),
                    ],
                    cwd=work / mode,
                    check=True,
                    timeout=60,
                )
                report[mode] = json.loads(output.read_bytes())
        assert report["native"]["contract"] == report["loader"]["contract"]
    args.output.write_text(json.dumps(report, indent=2), encoding="utf8")


if __name__ == "__main__":
    main()
