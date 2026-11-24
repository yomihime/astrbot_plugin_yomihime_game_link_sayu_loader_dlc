"""Same-input observations for the framework configuration store."""

from contextlib import nullcontext
from functools import partial
import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
from unittest.mock import patch


def builtin(value):
    from collections.abc import Mapping

    return (
        {key: builtin(item) for key, item in value.items()}
        if isinstance(value, Mapping)
        else value
    )


def observe(work, archive, existing):
    def offline(event, _args):
        if event in {
            "socket.connect",
            "socket.getaddrinfo",
            "socket.bind",
            "subprocess.Popen",
        }:
            raise PermissionError("offline configuration store")

    sys.addaudithook(offline)
    sys.path.insert(0, str(archive or work))
    data = work / "data"
    data.mkdir(exist_ok=True)
    scope = "probe"
    core = data / "config.json"
    fixture = {"plugins": {scope: {"priority": 7}}, "contract_marker": "preserve"}
    seed = json.dumps(fixture).encode("utf8")
    core.write_bytes(seed)
    per_plugin = data / "plugins_configs" / "probe.json"
    per_plugin.parent.mkdir(exist_ok=True)
    if existing:
        per_plugin.write_text('{"priority": 3}', encoding="utf8")
    if archive:
        from sayu_compat import flush_loaded_service_settings, install
        from service_settings import management_scope

        install(scope, "cp", data, source_root=work, runtime_mode="package")
        writable = partial(management_scope, writable=True)
    else:
        writable = nullcontext
    from gsuid_core.config import core_config, plugin_config_store as store

    def disk():
        if archive:
            from contextlib import closing
            import sqlite3

            path = data / "services.sqlite3"
            if not path.is_file():
                return None
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                row = db.execute(
                    "SELECT payload FROM profile_service_config WHERE name=?", (scope,)
                ).fetchone()
                return json.loads(row[0]) if row else None
        return json.loads(per_plugin.read_bytes()) if per_plugin.exists() else None

    with writable():
        if archive:
            flush_loaded_service_settings()
        result = {
            "migration": [
                builtin(store.get(scope)),
                disk(),
                core.with_name("config_backup.json").read_bytes() == seed,
                "plugins" not in json.loads(core.read_bytes()),
                json.loads(core.read_bytes())["contract_marker"],
            ]
        }
        top = store.get_all()
        old = store.get(scope)
        passed = {
            "enabled": True,
            "priority": 5,
            "sv": {"query": {"enabled": True, "priority": 6}},
        }
        store.set(scope, passed)
        current = store.get(scope)
        passed["priority"] = 91
        aliases_input = store.get(scope)["priority"] == 91
        current["priority"] = 5
        result["memory"] = [old is current, top[scope] is current, disk()["priority"]]
        store.save_dirty()
        result["saved_set"] = disk()
        current["priority"] = 2
        nested = current["sv"]["query"]
        nested["enabled"] = False
        result["nested"] = [
            store.get_all()[scope]["sv"]["query"] is nested,
            builtin(store.get(scope)),
            disk(),
        ]
        store.save_dirty()
        result["unmarked"] = disk()
        store.mark_dirty(scope)
        core_config.lazy_write_config()
        result["core_flush"] = disk()
        top[scope]["sv"]["query"]["priority"] = 12
        store.save_all()
        result["save_all"] = disk()
        store.get(scope)["priority"] = 9
        store.save(scope)
        result["save"] = disk()
        old["priority"] = 100
        result["detached"] = [old["priority"], store.get(scope)["priority"]]
        store.set(scope, {"priority": 8})
        fault = (
            "plugin_service_store.sqlite3.connect"
            if archive
            else "gsuid_core.config.atomic_save"
        )
        with (
            patch(fault, side_effect=OSError("fixture write failure")),
            patch("time.sleep"),
        ):
            try:
                store.save_dirty()
            except OSError:
                result["save_failure"] = ["OSError"]
            else:
                raise AssertionError("injected persistence failure was invisible")
        result["save_failure"].append(disk()["priority"])
        store.save_dirty()
        result["save_failure"].append(disk()["priority"])
        del store.get_all()[scope]
        source = work / "plugins" / scope / "__init__.py"
        source.parent.mkdir(parents=True)
        source.write_text(
            'from gsuid_core.sv import Plugins, SV\np = Plugins("probe", force_prefix=["cp"])\ns = SV("trace", pm=0)\n',
            encoding="utf8",
        )
        registered = runpy.run_path(str(source))
        plugin, service = registered["p"], registered["s"]
        store.save_all()
        stored_priority = disk()["sv"]["trace"]["priority"]
        service.set(priority=11)
        lazy = [service.priority, disk()["sv"]["trace"]["priority"]]
        store.save_dirty()
        service.set(False, priority=4)
        immediate = [service.priority, disk()["sv"]["trace"]["priority"]]
        result["registered_set"] = [
            stored_priority,
            lazy,
            immediate,
            plugin.enabled,
            service.pm,
        ]
        store.get(scope)["sv"]["trace"]["enabled"] = False
        live_after_raw_edit = service.enabled
    return {
        "shared": result,
        "aliases_set_argument": aliases_input,
        "registered_enabled_after_raw_disable": live_after_raw_edit,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--child", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--archive", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--existing", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    path = args.output.resolve()
    if args.child:
        path.write_text(
            json.dumps(
                observe(args.child, args.archive, args.existing), ensure_ascii=False
            ),
            encoding="utf8",
        )
        return
    script = Path(__file__).resolve()
    sys.path.insert(0, str(script.parent))
    from native_contract import REFERENCE_COMMIT, copy_reference
    from build_local import worker_archive

    report = {"python": sys.version.split()[0]}
    for label, existing in (("legacy_only", False), ("existing_file", True)):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            output = work / "observed.json"
            args_child = [
                sys.executable,
                "-I",
                "-B",
                "-X",
                "utf8",
                str(script),
                "--child",
                tmp,
                "--output",
                str(output),
            ]
            if existing:
                args_child.append("--existing")
            if args.reference:
                report["runtime"] = {
                    "reference": REFERENCE_COMMIT,
                    **copy_reference(args.reference.resolve(), work),
                }
            else:
                lock = json.loads(
                    (script.parents[1] / "profiles/lock.json").read_bytes()
                )["starrail"]
                worker = worker_archive(lock)
                archive = work / "worker.pyz"
                archive.write_bytes(worker)
                args_child += ["--archive", str(archive)]
                report["runtime"] = {
                    "worker_sha256": hashlib.sha256(worker).hexdigest()
                }
            subprocess.run(
                args_child,
                cwd=work,
                check=True,
                env={**os.environ, "HOME": tmp, "USERPROFILE": tmp},
            )
            report[label] = json.loads(output.read_bytes())
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    if args.compare:
        oracle = json.loads(args.compare.read_bytes())
        for label in ("legacy_only", "existing_file"):
            assert report[label]["shared"] == oracle[label]["shared"], (
                label,
                report[label],
                oracle[label],
            )
        print("matched both migration paths and every shared configuration observation")


if __name__ == "__main__":
    main()
