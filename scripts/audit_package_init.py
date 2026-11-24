"""Locate the first ordinary package import gap in each pinned nested game archive."""

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
from collections import Counter, deque
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import traceback
import zipfile
from urllib.parse import urlsplit
from urllib.request import url2pathname


ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".sayu-local"


def guard_files(source, roots, *, networking_guarded=False):
    """Reject mutating operations outside allowed temporary directories and network/process actions."""

    source_operations = Counter()
    writable_paths = deque(maxlen=128)

    def check(event, args):
        if event.startswith("socket.") and not networking_guarded:
            raise PermissionError(event)
        if event.startswith(
            (
                "subprocess.",
                "ctypes.",
                "os.spawn",
                "os.exec",
                "os.posix_spawn",
                "os.fork",
                "os.system",
                "os.startfile",
                "winreg.",
            )
        ):
            raise PermissionError(event)
        if event == "import" and len(args) > 1 and isinstance(args[1], str):
            native = Path(args[1])
            if native.suffix.lower() in {
                ".pyd",
                ".dll",
                ".so",
            } and native.resolve().is_relative_to(source):
                raise PermissionError("native extension in staged package")
        paths = ()
        if event == "open":
            path, _, flags = args
            if flags & (
                os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
            ):
                paths = (path,)
        elif event == "sqlite3.connect":
            if args[0] == ":memory:":
                return
            database = os.fsdecode(args[0])
            if database.startswith("file:"):
                uri = urlsplit(database)
                if uri.netloc or uri.fragment:
                    raise PermissionError("non-local SQLite URI")
                database = url2pathname(uri.path)
            paths = (database,)
        elif event in {
            "os.remove",
            "os.rmdir",
            "os.mkdir",
            "os.chmod",
            "os.utime",
            "os.truncate",
        }:
            paths = args[:1]
        elif event in {"os.rename", "os.link", "os.symlink"}:
            paths = args[:2]
        for raw in paths:
            if event == "open" and isinstance(raw, int):
                # os.fdopen wraps the handle from a preceding os.open. Accept
                # only a regular file still identifiable inside an allowed root.
                descriptor = os.fstat(raw)
                if descriptor.st_ino and stat.S_ISREG(descriptor.st_mode):
                    for item in reversed(tuple(writable_paths)):
                        try:
                            if any(
                                item.resolve().is_relative_to(root) for root in roots
                            ) and os.path.samestat(descriptor, item.stat()):
                                return
                        except OSError:
                            continue
            if not isinstance(raw, (str, bytes)):
                raise PermissionError(f"{event} requires an auditable path")
            path = Path(os.fsdecode(raw)).resolve()
            if path.is_relative_to(source):
                # pathlib.mkdir(exist_ok=True) issues a syscall even for an
                # already existing directory; this cannot create or change it.
                if event == "os.mkdir" and path.is_dir():
                    continue
                relative = path.relative_to(source).as_posix()
                source_operations[(event, relative)] += 1
            if not any(path.is_relative_to(root) for root in roots):
                raise PermissionError(f"{event} outside allowed temporary directories")
            if event == "open":
                writable_paths.append(path)

    sys.addaudithook(check)
    return source_operations


async def inspect_package(package, prefix, writable_source, lifecycle=False):
    work = Path.cwd()
    source = work / "plugins"
    data = work / "data"
    data.mkdir()
    sys.path.insert(0, str(work / "worker.pyz"))
    import game_profile
    import sayu_compat
    from plugin_runtime import PluginRuntime
    from service_settings import management_scope
    from worker_storage import install_offline_guard

    if (package, prefix, game_profile.RUNTIME_MODE) != (
        game_profile.PACKAGE, game_profile.PREFIX, "package"
    ):
        raise ValueError("audit profile differs from the packed worker")
    import_errors = []

    def record_error(name, exc):
        frames = [
            {
                "file": Path(frame.filename).relative_to(source).as_posix(),
                "line": frame.lineno,
            }
            for frame in traceback.extract_tb(exc.__traceback__)
            if Path(frame.filename).is_relative_to(source)
        ]
        import_errors.append(
            {
                "name": name,
                "type": type(exc).__name__,
                "detail": str(exc),
                "frames": frames,
            }
        )

    runtime = PluginRuntime(data, on_import_error=record_error)
    downloads = sayu_compat.install(package, prefix, data, source_root=source)
    if sys.platform == "win32" and importlib.util.find_spec("truststore"):
        # The pinned HTTP client binds Windows certificate APIs at import time.
        # Load the dependency before applying the offline plugin audit.
        importlib.import_module("truststore")
    install_offline_guard()
    operations = guard_files(
        source, (data, source) if writable_source else (data,), networking_guarded=True
    )
    phase = "startup" if lifecycle else "import"
    try:
        with management_scope(writable=True):
            if lifecycle:
                await runtime.start(source, package)
                phase = "shutdown"
                await runtime.stop()
            else:
                runtime.import_package(source, package)
    except Exception as exc:
        frames = [
            {
                "file": Path(frame.filename).relative_to(source).as_posix(),
                "line": frame.lineno,
            }
            for frame in traceback.extract_tb(exc.__traceback__)
            if Path(frame.filename).is_relative_to(source)
        ]
        row = {
            "status": phase + "_blocked",
            "type": type(exc).__name__,
            "detail": str(exc),
            "frames": frames,
            "partial_registrations": sayu_compat.registration_counts(),
        }
    else:
        row = {
            "status": "started_and_stopped" if lifecycle else "imported",
            "registered": sayu_compat.registration_counts(),
        }
        if lifecycle:
            row["migration_result"] = runtime.orm.report
    if import_errors and row["status"] in ("imported", "started_and_stopped"):
        row["status"] += "_with_module_errors"
    row["modules"] = runtime.modules
    row["import_errors"] = import_errors
    row["failed_hook_phases"] = runtime.hook_errors
    row["downloads"] = downloads.mirror.diagnostics()
    from gsuid_core.logger import logger

    diagnostics = logger._diagnostics
    with diagnostics.lock:
        row["log_counts"] = dict(diagnostics.counts)
        row["recent_warnings_and_errors"] = [
            (level, site)
            for level, site in diagnostics.recent
            if level in ("WARNING", "ERROR", "CRITICAL")
        ]
    row["source_write_attempts"] = [
        {"event": event, "path": path, "count": count}
        for (event, path), count in sorted(operations.items())
    ]
    row["lifecycle_registrations"] = {
        "before": len(runtime.before_hooks),
        "start": len(runtime.start_hooks),
        "shutdown": len(runtime.shutdown_hooks),
        "jobs": len(runtime.scheduler.get_jobs()),
    }
    return row


def audit(writable_source, profile=None, lifecycle=False):
    from environments import selected
    from build_local import worker_archive

    specs = json.loads((ROOT / "profiles/lock.json").read_bytes())
    report = {}
    for key, spec in specs.items():
        if profile is not None and key != profile:
            continue
        plugin = spec["package"]
        digest = spec["snapshot_sha256"]
        archive = LOCAL / "runtime" / f"{plugin}-{digest}.zip"
        exe, _ = selected(
            LOCAL / key, (ROOT / "profiles" / f"{key}-requirements.txt").read_bytes()
        )
        with tempfile.TemporaryDirectory(prefix="sayu-import-probe-") as tmp:
            work = Path(tmp)
            (work / "worker.pyz").write_bytes(worker_archive(spec, offline=True))
            with archive.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                    raise ValueError("package digest mismatch")
                stream.seek(0)
                with zipfile.ZipFile(stream) as z:
                    z.extractall(work / "plugins")
            environment = {
                k: v
                for k, v in os.environ.items()
                if k.upper() in {"SYSTEMROOT", "WINDIR", "LANG"}
            }
            environment.update(TMP=tmp, TEMP=tmp, TMPDIR=tmp)
            try:
                child = subprocess.run(
                    [
                        exe,
                        "-I",
                        "-B",
                        "-X",
                        "utf8",
                        __file__,
                        "--package",
                        plugin,
                        "--prefix",
                        spec["prefix"],
                        *(["--writable-source"] if writable_source else []),
                        *(["--lifecycle"] if lifecycle else []),
                    ],
                    cwd=work,
                    env=environment,
                    capture_output=True,
                    timeout=45,
                    check=False,
                )
                if child.returncode:
                    row = {"status": "probe_exited", "exit_code": child.returncode}
                else:
                    row = json.loads(child.stdout.decode("utf-8").splitlines()[-1])
            except subprocess.TimeoutExpired:
                row = {"status": "probe_timeout"}
            report[key] = {
                "worker_sha256": hashlib.sha256(
                    (work / "worker.pyz").read_bytes()
                ).hexdigest(),
                "snapshot_sha256": digest,
                "requirements_sha256": hashlib.sha256(
                    (ROOT / "profiles" / f"{key}-requirements.txt").read_bytes()
                ).hexdigest(),
                "writable_staged_source": writable_source,
                **row,
            }
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--profile", choices=json.loads((ROOT / "profiles/lock.json").read_bytes())
    )
    parser.add_argument(
        "--writable-source",
        action="store_true",
        help="permit writes to the disposable source copy",
    )
    parser.add_argument("--package", help=argparse.SUPPRESS)
    parser.add_argument(
        "--lifecycle",
        action="store_true",
        help="also run real startup, migrations, scheduling and shutdown",
    )
    parser.add_argument("--prefix", help=argparse.SUPPRESS)
    args = parser.parse_args()
    result = (
        asyncio.run(
            inspect_package(
                args.package, args.prefix, args.writable_source, args.lifecycle
            )
        )
        if args.package
        else audit(args.writable_source, args.profile, args.lifecycle)
    )
    encoded = json.dumps(result, ensure_ascii=True)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    else:
        print(encoded)
