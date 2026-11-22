"""Locate the first ordinary package import gap in each pinned nested game archive."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback
import zipfile


ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".sayu-local"


def guard_files(source, roots):
    """Reject mutating operations outside allowed temporary directories and network/process actions."""

    def check(event, args):
        if event.startswith(
            (
                "socket.",
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
            paths = args[:1]
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
        if any(
            not isinstance(p, (str, bytes))
            or not any(
                Path(os.fsdecode(p)).resolve().is_relative_to(root) for root in roots
            )
            for p in paths
        ):
            raise PermissionError(f"{event} outside allowed temporary directories")

    sys.addaudithook(check)


def inspect_package(package, prefix, writable_source):
    work = Path.cwd()
    source = work / "plugins"
    data = work / "data"
    data.mkdir()
    sys.path.insert(0, str(ROOT / "worker"))
    import sayu_compat
    from plugin_runtime import PluginRuntime

    runtime = PluginRuntime(data)
    sayu_compat.install(package, prefix, data, source_root=source)
    guard_files(source, (data, source) if writable_source else (data,))
    try:
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
        return {
            "status": "import_blocked",
            "type": type(exc).__name__,
            "detail": str(exc),
            "frames": frames,
            "partial_registrations": sayu_compat.registration_counts(),
        }
    return {"status": "imported", "registered": sayu_compat.registration_counts()}


def audit(writable_source):
    from environments import selected

    specs = json.loads((ROOT / "profiles/lock.json").read_bytes())
    report = {}
    for key, spec in specs.items():
        plugin = spec["package"]
        digest = spec["snapshot_sha256"]
        archive = LOCAL / "runtime" / f"{plugin}-{digest}.zip"
        exe, _ = selected(
            LOCAL / key, (ROOT / "profiles" / f"{key}-requirements.txt").read_bytes()
        )
        with tempfile.TemporaryDirectory(prefix="sayu-import-probe-") as tmp:
            work = Path(tmp)
            with archive.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                    raise ValueError("package digest mismatch")
                stream.seek(0)
                with zipfile.ZipFile(stream) as z:
                    z.extractall(work / "plugins" / plugin)
            environment = {
                k: v
                for k, v in os.environ.items()
                if k.upper() in {"SYSTEMROOT", "WINDIR", "LANG"}
            }
            environment.update(TMP=tmp, TEMP=tmp, TMPDIR=tmp)
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
                ],
                cwd=work,
                env=environment,
                capture_output=True,
                timeout=45,
                check=True,
            )
            row = json.loads(child.stdout.decode("utf-8").splitlines()[-1])
            report[key] = {
                "snapshot_sha256": digest,
                "writable_staged_source": writable_source,
                **row,
            }
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--writable-source",
        action="store_true",
        help="permit writes to the disposable source copy",
    )
    parser.add_argument("--package", help=argparse.SUPPRESS)
    parser.add_argument("--prefix", help=argparse.SUPPRESS)
    args = parser.parse_args()
    result = (
        inspect_package(args.package, args.prefix, args.writable_source)
        if args.package
        else audit(args.writable_source)
    )
    encoded = json.dumps(result, ensure_ascii=True)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    else:
        print(encoded)
