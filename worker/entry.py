"""Serve requests on a single event loop until this module generation retires."""

import asyncio
import errno
import json
import os
import sys
from collections import Counter
from pathlib import Path

import sayu_compat
from declarations import Unsupported
from discovery import Discovery, discover
from game_profile import PACKAGE, PREFIX, RUNTIME_MODE
from images import prepare_owner_parts, prepare_public_parts
from plugin_runtime import PluginRuntime
from plugin_bindings import binding_scope
from service_settings import management_scope


class Worker:
    def __init__(self, source, data):
        self.source = source
        self.data = data
        self.skipped = Counter()
        self.plugin = None
        self.discovery = None

    async def start(self):
        sayu_compat.install(PACKAGE, PREFIX, self.data, source_root=self.source)
        if RUNTIME_MODE == "package":
            self.plugin = PluginRuntime(self.data)
            with management_scope(writable=True):
                await self.plugin.start(self.source, PACKAGE)
        elif RUNTIME_MODE == "declarations":
            # Declaration-only profiles do not execute the package directory initializer.
            sayu_compat.get_res_path(PACKAGE)
            self.discovery = Discovery(self.source, PACKAGE)
            await discover(
                self.source, PACKAGE, skipped=self.skipped, discovery=self.discovery
            )
        else:
            raise ValueError("unknown worker runtime mode")

    async def handle(self, request):
        if request.get("op") == "inventory":
            report = {
                "mode": RUNTIME_MODE,
                "adapter_log_counts": sayu_compat.adapter_log_counts(),
                "registered": sayu_compat.registration_counts(),
                "skipped": dict(self.skipped),
                "failed_jobs": sorted(self.plugin.failed_jobs) if self.plugin else [],
            }
            if request.get("list_triggers"):
                report["triggers"] = sayu_compat.registered_triggers()
            return report
        if request.get("op") is not None:
            raise ValueError("unknown operation")
        if self.discovery is not None:
            self.discovery.assets.begin_invocation()
        event = request["event"]
        with (
            management_scope(),
            binding_scope(
                event.get("actor_id"), event.get("adapter_id"), request["private_image"]
            ),
        ):
            parts = await sayu_compat.dispatch(request["text"], **event)
        if request["private_image"]:
            parts = prepare_owner_parts(parts, self.source, PACKAGE)
        else:
            parts = prepare_public_parts(parts, self.source, PACKAGE)
        return {"parts": parts}


def send(output, message):
    data = json.dumps(message, ensure_ascii=True, allow_nan=False).encode("ascii")
    if len(data) > 6 * 1024 * 1024:
        data = b'{"error":"unsupported_message"}'
    output.write(len(data).to_bytes(4, "big") + data)
    output.flush()


def error_code(exc):
    if isinstance(exc, sayu_compat.NoMatch):
        return "no_match"
    if isinstance(exc, sayu_compat.UnsupportedMessage):
        return "unsupported_message"
    if isinstance(exc, (ImportError, NotImplementedError, Unsupported)):
        return "missing_port"
    return "upstream_error"


async def serve(output):
    worker = Worker(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
    try:
        await worker.start()
    except Exception as exc:
        send(
            output,
            {"error": error_code(exc), "phase": "startup", "type": type(exc).__name__},
        )
        return
    send(output, {"ready": True, "mode": RUNTIME_MODE})
    try:
        while True:
            line = await asyncio.to_thread(sys.stdin.buffer.readline, 16385)
            if not line:
                break
            if not line.endswith(b"\n") or len(line) > 16384:
                raise ValueError("invalid request frame")
            try:
                reply = await worker.handle(json.loads(line))
            except Exception as exc:
                reply = {"error": error_code(exc)}
            send(output, reply)
    finally:
        if worker.plugin:
            await worker.plugin.stop()


class ProfileBusy(RuntimeError):
    pass


def claim_profile(data):
    """Keep the OS lock across plugin shutdown and executor/thread retirement."""
    data.mkdir(parents=True, exist_ok=True)
    handle = (data / ".worker.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            raise ProfileBusy from exc
        raise
    return handle


def main():
    # Keep even imports and native writes to stdout off the protocol stream.
    with os.fdopen(os.dup(sys.stdout.fileno()), "wb") as output:
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        try:
            lock = claim_profile(Path(sys.argv[2]).resolve())
        except (OSError, ProfileBusy) as exc:
            send(
                output,
                {
                    "error": "profile_busy"
                    if isinstance(exc, ProfileBusy)
                    else "profile_unavailable",
                    "phase": "startup",
                    "type": type(exc).__name__,
                },
            )
            return
        with lock:
            asyncio.run(serve(output))


if __name__ == "__main__":
    main()
