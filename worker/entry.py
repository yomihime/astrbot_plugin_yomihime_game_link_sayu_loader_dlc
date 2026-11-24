"""Serve requests on a single event loop until this module generation retires."""

import asyncio
from contextlib import nullcontext
import errno
import json
import os
import sys
from pathlib import Path

import sayu_compat
import game_profile
from port_errors import Unsupported
from game_profile import OFFLINE, OWNER_WRITES, PACKAGE, PREFIX, RUNTIME_MODE
from images import prepare_owner_parts, prepare_public_parts
from plugin_runtime import PluginRuntime
from plugin_bindings import binding_scope
from service_settings import management_scope
from worker_storage import install_write_guard


class Worker:
    def __init__(self, source, data):
        self.source = source
        self.data = data
        self.plugin = PluginRuntime(self.data)
        self.reads = None

    async def start(self):
        self.downloads = sayu_compat.install(
            PACKAGE, PREFIX, self.data, source_root=self.source
        )
        if RUNTIME_MODE != "package":
            raise ValueError("package initialization is required")
        from resource_broker import CLIENT, startup_resources

        if CLIENT is not None:
            from resource_demand import ResourceReads, install_read_audit

            self.reads = ResourceReads(self.downloads, game_profile, CLIENT)
            self.downloads.mirror.demand = self.reads
            install_read_audit()

        with management_scope(writable=True), startup_resources():
            await self.plugin.start(self.source, PACKAGE)

    async def handle(self, request):
        if request.get("op") == "inventory":
            report = {
                "mode": RUNTIME_MODE,
                "adapter_log_counts": sayu_compat.adapter_log_counts(),
                "registered": sayu_compat.registration_counts(),
                "modules": self.plugin.modules,
                "failed_jobs": sorted(self.plugin.failed_jobs),
                "failed_hook_phases": dict(self.plugin.hook_errors),
                "database_migrations": dict(self.plugin.orm.report),
                "downloads": self.downloads.mirror.diagnostics(),
            }
            if request.get("list_triggers"):
                report["triggers"] = sayu_compat.registered_triggers()
            from resource_broker import CLIENT

            if CLIENT is not None:
                report["public_resource_startup"] = CLIENT.startup_diagnostics
                report["public_static_httpx"] = list(getattr(CLIENT, "static_httpx", ()))
            return report
        if request.get("op") is not None:
            raise ValueError("unknown operation")
        event = request["event"]
        owner_action = (
            request.get("owner_action") is True
            and request.get("private_image") is True
            and request.get("capability_id") in OWNER_WRITES
            and event.get("conversation_kind") == "direct"
            and not event.get("group_id")
            and bool(event.get("actor_id") and event.get("adapter_id"))
        )
        with (
            management_scope(),
            self.reads.scope() if self.reads is not None else nullcontext(),
            binding_scope(
                event.get("actor_id"),
                event.get("adapter_id"),
                request["private_image"],
                preferences=owner_action,
                uid_read=owner_action,
                uid_write=owner_action,
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
    if OFFLINE or getattr(game_profile, "PUBLIC_RESOURCES_ONLY", False):
        from worker_storage import install_offline_guard

        install_offline_guard()
    worker = Worker(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
    try:
        await worker.start()
    except Exception as exc:
        send(
            output,
            {"error": error_code(exc), "phase": "startup", "type": type(exc).__name__},
        )
        return
    send(
        output,
        {
            "ready": True,
            "mode": RUNTIME_MODE,
            "failed_modules": sum(
                error is not None for error in worker.plugin.modules.values()
            ),
        },
    )
    try:
        worker.downloads.mirror.start_continuations()
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
        from resource_broker import CLIENT

        try:
            if worker.reads is not None:
                worker.reads.close()
            await worker.downloads.mirror.stop_continuations()
            if worker.plugin:
                await worker.plugin.stop()
        finally:
            if CLIENT is not None:
                CLIENT.close()


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
            from resource_broker import activate

            resource_mode = getattr(game_profile, "PUBLIC_RESOURCES_ONLY", False)
            with (
                activate(Path(sys.argv[0]).resolve())
                if resource_mode
                else nullcontext()
            ):
                install_write_guard(Path(sys.argv[1]).resolve().parent, sys.argv[2])
                if resource_mode:
                    from resource_broker import CLIENT
                    from resource_httpx import static_httpx

                    with static_httpx(game_profile, CLIENT):
                        asyncio.run(serve(output))
                else:
                    asyncio.run(serve(output))


if __name__ == "__main__":
    main()
