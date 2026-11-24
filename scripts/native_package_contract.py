"""Run each pinned game package under the original GsCore module importer and hooks."""

import argparse
import asyncio
import hashlib
import importlib
import inspect
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_package_init import guard_files
from environments import selected
from native_contract import ROOT, REFERENCE_COMMIT, copy_reference, offline


LOCAL = ROOT / ".sayu-local"
MARKER = "native-package-progress.json"


def public_error(exc, work, source):
    frames = []
    dependencies = []
    for frame in traceback.extract_tb(exc.__traceback__):
        file = Path(frame.filename)
        if file.is_relative_to(source):
            frames.append(
                {"file": file.relative_to(source).as_posix(), "line": frame.lineno}
            )
        elif "/site-packages/" in file.as_posix():
            dependencies.append(
                {
                    "file": file.as_posix().split("/site-packages/")[-1],
                    "line": frame.lineno,
                }
            )
    return {
        "type": type(exc).__name__,
        "detail": str(exc)
        .replace(repr(str(work))[1:-1], "<temporary>")
        .replace(str(work), "<temporary>")[:600],
        "frames": frames,
        "dependency_frames": dependencies[-4:],
    }


async def child(package, libraries):
    work = Path.cwd()
    source = work / "gsuid_core" / "plugins"
    state_dir = work / "data"
    state_dir.mkdir(exist_ok=True)
    out = state_dir / MARKER
    sys.path.insert(0, str(work))
    # GsServer.load_plugins adds this directory before dispatching to load_plugin.
    sys.path.append(str(work / "gsuid_core"))
    sys.path.append(str(libraries))
    sys.addaudithook(offline)
    from gsuid_core.utils.database import base_models as database

    await database.init_database()
    from gsuid_core import server as module
    from gsuid_core.gss import gss
    from gsuid_core.sv import SL
    from gsuid_core.aps import scheduler, start_scheduler, shutdown_scheduler
    from gsuid_core.utils.database import startup  # noqa: F401 -- register native before hooks
    from gsuid_core.ai_core.heavy_import_gate import install_ai_off_heavy_gate

    if sys.platform == "win32" and importlib.util.find_spec("truststore"):
        importlib.import_module("truststore")
    # Locked HTTP libraries initialize their Windows or Brotli native bindings.
    # Load those trusted dependencies before denying all package-level FFI.
    if importlib.util.find_spec("httpx"):
        importlib.import_module("httpx")
    install_ai_off_heavy_gate()
    module.refresh_installed_dependencies()
    gss.is_load = True
    operations = guard_files(source, (state_dir, source, work / "private-home"))
    errors = []
    report = {
        "entries": [],
        "stages": [],
        "native_errors": errors,
        "comparison_probes": {},
    }

    def save():
        out.write_text(json.dumps(report, ensure_ascii=True), encoding="utf8")

    class Capture(logging.Handler):
        def emit(self, record):
            exc = record.exc_info[1] if record.exc_info else sys.exception()
            if exc is not None:
                errors.append(
                    {
                        "stage": report.get("active_stage"),
                        **public_error(exc, work, source),
                    }
                )
            else:
                message = record.msg
                value = (
                    message.get("event", "")
                    if isinstance(message, dict)
                    else str(message)
                )
                errors.append(
                    {
                        "stage": report.get("active_stage"),
                        "detail": str(value).replace(str(work), "<temporary>")[:300],
                    }
                )

    capture = Capture(logging.ERROR)
    logging.getLogger().addHandler(capture)

    async def stage(name, action):
        report["active_stage"] = name
        save()
        begin = len(errors)
        try:
            await action()
        except Exception as exc:
            errors.append({"stage": name, **public_error(exc, work, source)})
        report["stages"].append(
            {"name": name, "logged_or_raised_errors": len(errors) - begin}
        )
        save()

    report["active_stage"] = "discover"
    entries = gss.load_plugin(source / package)
    if not isinstance(entries, list):
        raise RuntimeError(f"native discovery failed: {entries}")
    report["discovered_count"] = len(entries)
    report["active_stage"] = "import"
    save()
    for name, path, kind in entries:
        start = len(errors)
        # GsServer catches individual module failures and proceeds to the next.
        # Invoke that real entry point one module at a time to bind each failure.
        gss._import_module_list([(name, path, kind)])
        row = {"name": name, "imported": name in sys.modules}
        if errors[start:]:
            row["errors"] = errors[start:].copy()
        report["entries"].append(row)
        save()

    if package == "GenshinUID":
        try:
            from gsuid_core.utils.api.mys.api import GET_GACHA_LOG_URL  # noqa: F401
        except ImportError as exc:
            probe = public_error(exc, work, source)
            probe["statement"] = (
                "from gsuid_core.utils.api.mys.api import GET_GACHA_LOG_URL"
            )
            report["comparison_probes"]["legacy_gacha_import"] = probe

    triggers = [
        (service, trigger)
        for service in SL.lst.values()
        for mapping in service.TL.values()
        for trigger in mapping.values()
    ]
    report["native_registered"] = {
        "services": len(SL.lst),
        "services_with_routes": len({id(service) for service, _ in triggers}),
        "handlers": len({inspect.unwrap(trigger.func) for _, trigger in triggers}),
        "triggers_before_prefix_expansion": len(
            {
                (
                    id(service),
                    trigger.type,
                    trigger.keyword,
                    inspect.unwrap(trigger.func),
                )
                for service, trigger in triggers
            }
        ),
        "expanded_triggers": len(triggers),
        "jobs": len(scheduler.get_jobs()),
        "before": len(module.core_start_before_def),
        "start": len(module.core_start_def),
        "stop": len(module.core_shutdown_def),
    }
    await stage("before_start", module.core_start_before_execute)
    await stage("start", module.core_start_execute)
    await stage("scheduler_start", start_scheduler)
    report["scheduler_started"] = scheduler.running
    await stage("scheduler_stop", shutdown_scheduler)
    await stage("shutdown", module.core_shutdown_execute)
    if database.engine is not None:
        await database.engine.dispose()
    report["failed_modules"] = [
        entry["name"] for entry in report["entries"] if not entry["imported"]
    ]
    report["source_write_attempts"] = [
        {"op": op, "path": path, "count": n}
        for (op, path), n in sorted(operations.items())
    ]
    report["status"] = (
        "completed_with_errors" if errors or report["failed_modules"] else "completed"
    )
    report.pop("active_stage", None)
    save()
    logging.getLogger().removeHandler(capture)
    logging.shutdown()


def run_one(key, spec, args):
    requirements = (ROOT / "profiles" / f"{key}-requirements.txt").read_bytes()
    exe, env_info = selected(LOCAL / key, requirements)
    libraries = (
        exe.parent.parent / "Lib" / "site-packages"
        if os.name == "nt"
        else next((exe.parent.parent / "lib").glob("python*/site-packages"))
    )
    digest = spec["snapshot_sha256"]
    archive = LOCAL / "runtime" / f"{spec['package']}-{digest}.zip"
    with tempfile.TemporaryDirectory(prefix=f"sayu-native-{key}-") as tmp:
        work = Path(tmp)
        provenance = copy_reference(args.reference, work)
        source = work / "gsuid_core" / "plugins"
        source.mkdir(exist_ok=True)
        with archive.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise ValueError("package digest mismatch")
            stream.seek(0)
            with ZipFile(stream) as zipped:
                zipped.extractall(source)
        home = work / "private-home"
        home.mkdir()
        environment = {
            k: v
            for k, v in os.environ.items()
            if k.upper() in {"SYSTEMROOT", "WINDIR", "LANG"}
        }
        environment.update(
            {
                k: str(home)
                for k in (
                    "HOME",
                    "USERPROFILE",
                    "APPDATA",
                    "LOCALAPPDATA",
                    "TEMP",
                    "TMP",
                    "TMPDIR",
                    "XDG_CACHE_HOME",
                )
            }
        )
        with (work / "process.log").open("wb") as log:
            try:
                proc = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        "-X",
                        "utf8",
                        __file__,
                        "--child",
                        spec["package"],
                        "--libraries",
                        str(libraries),
                    ],
                    cwd=work,
                    env=environment,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=args.timeout,
                    check=False,
                )
                code = proc.returncode
            except subprocess.TimeoutExpired:
                code = "timeout"
        progress = work / "data" / MARKER
        row = (
            json.loads(progress.read_bytes())
            if progress.exists()
            else {"status": "bootstrap_failed"}
        )
        if code != 0:
            row["status"] = "native_process_" + str(code)
            tail = (
                (work / "process.log")
                .read_bytes()[-4096:]
                .decode("utf8", "replace")
                .splitlines()[-10:]
            )
            row["process_tail"] = [
                line.replace(str(work), "<temporary>")[:500] for line in tail
            ]
        row.update(
            reference=REFERENCE_COMMIT,
            source_verification=provenance,
            package_commit=spec["commit"],
            snapshot_sha256=digest,
            requirements_sha256=hashlib.sha256(requirements).hexdigest(),
            extra_dependency_environment=env_info["environment"],
            native_runtime=sys.version,
            extra_dependency_runtime=env_info["base"][1],
            dependency_resolution="Reference environment first, profile site-packages second. Extension modules must be compatible with the reference interpreter.",
            harness_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            guard_sha256=hashlib.sha256(
                (ROOT / "scripts/audit_package_init.py").read_bytes()
            ).hexdigest(),
            process_result=code,
        )
        return row


def compare_receipts(native_file, loader_file):
    """Compare independently recorded observations from the same game archive."""
    native = json.loads(native_file.read_bytes())
    loader = json.loads(loader_file.read_bytes())
    result = {}
    for key in native.keys() & loader.keys():
        first, second = native[key], loader[key]
        if any(
            first[field] != second[field]
            for field in ("snapshot_sha256", "requirements_sha256")
        ):
            raise ValueError(f"{key}: mixed archive or profile requirements")
        if first["process_result"] != 0 or "modules" not in second:
            raise ValueError(f"{key}: incomplete observation")
        entries = {entry["name"]: entry for entry in first["entries"]}
        product_errors = {
            entry["name"]: entry for entry in second.get("import_errors", [])
        }
        modules = []
        for name in sorted(entries.keys() | second["modules"].keys()):
            original = entries.get(name)
            baseline = (
                "undiscovered"
                if original is None
                else "imported"
                if original["imported"]
                else "failed"
            )
            product = (
                "undiscovered"
                if name not in second["modules"]
                else "imported"
                if second["modules"][name] is None
                else "failed"
            )
            row = {"module": name, "native": baseline, "loader": product}
            if baseline == "failed" or product == "failed":
                expected = (original or {}).get("errors", [])
                actual = product_errors.get(name, {})
                row["native_errors"] = [
                    (e.get("type"), e.get("detail", "").split(" (", 1)[0])
                    for e in expected
                ]
                row["loader_error"] = (
                    actual.get("type"),
                    actual.get("detail", "").split(" (", 1)[0],
                )
                row["same_failure"] = row["loader_error"] in row["native_errors"]
            modules.append(row)
        totals = first["native_registered"]
        result[key] = {
            "snapshot_sha256": first["snapshot_sha256"],
            "requirements_sha256": first["requirements_sha256"],
            "native_receipt_sha256": hashlib.sha256(
                native_file.read_bytes()
            ).hexdigest(),
            "loader_receipt_sha256": hashlib.sha256(
                loader_file.read_bytes()
            ).hexdigest(),
            "module_observations": modules,
            "same_import_outcomes": all(
                e["native"] == e["loader"] and e.get("same_failure", True)
                for e in modules
            ),
            "native_routes": {
                "services": totals["services_with_routes"],
                "handlers": totals["handlers"],
                "triggers": totals["triggers_before_prefix_expansion"],
            },
            "loader_routes": second.get(
                "registered", second.get("partial_registrations", {})
            ),
            "native_startup": first["stages"],
            "loader_lifecycle": second["status"],
            "loader_log_counts": second.get("log_counts", {}),
            "scope": "Route totals and module outcomes are independent measurements. Lifecycle errors have different reporting boundaries. Neither process contacted a live endpoint or adapter.",
        }
    if not result:
        raise ValueError("no profile shared by these receipts")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, default=LOCAL / "reference")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--profile", choices=("genshin", "starrail", "waves"))
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--child")
    parser.add_argument("--libraries", type=Path)
    parser.add_argument("--compare-native", type=Path)
    parser.add_argument("--compare-loader", type=Path)
    args = parser.parse_args()
    if args.child:
        asyncio.run(child(args.child, args.libraries))
    elif args.compare_native or args.compare_loader:
        if not (args.compare_native and args.compare_loader and args.output):
            parser.error("comparison requires both receipts and --output")
        report = compare_receipts(args.compare_native, args.compare_loader)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf8")
        for key, row in report.items():
            print(key, "same import outcomes:", row["same_import_outcomes"])
        if any(
            not row["same_import_outcomes"]
            or row["native_routes"] != row["loader_routes"]
            for row in report.values()
        ):
            raise SystemExit(1)
    else:
        specs = json.loads((ROOT / "profiles/lock.json").read_bytes())
        report = {}
        for key, spec in specs.items():
            if args.profile and args.profile != key:
                continue
            report[key] = run_one(key, spec, args)
            if args.output:
                args.output.write_text(json.dumps(report, indent=2), encoding="utf8")
            print(
                key,
                report[key]["status"],
                len(report[key].get("failed_modules", [])),
                flush=True,
            )
