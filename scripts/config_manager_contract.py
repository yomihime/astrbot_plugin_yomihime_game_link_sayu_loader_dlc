"""Exercise the pinned and packaged configuration managers in separate private processes."""

import argparse
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def config_cases():
    from gsuid_core.aps import scheduler
    from gsuid_core.config import core_config
    from gsuid_core.data_store import get_res_path
    from gsuid_core.utils.plugins_config.gs_config import ConfigSetManager, StringConfig
    from gsuid_core.utils.plugins_config.models import GsStrConfig

    root = get_res_path("config-sets-probe")
    before = {job.id for job in scheduler.get_jobs()}

    def template(title="Title"):
        return {"field": GsStrConfig(title, "Desc", "初始", secret=True)}

    base = root / "flat"
    m = ConfigSetManager(base, template(), "_probe")
    rows = {"before": [base.exists(), m.list_available(), m.exists("a")]}
    rows["create"] = [
        m.create_default("b"),
        m.create_default("a"),
        m.create_default("a"),
    ]
    a = m.get_config("a")
    rows["cache"] = [m.get_config("a") is a, a.config_name, list(m.get_all_configs())]
    disk = base / "a.json"

    def data(path):
        return json.loads(path.read_bytes())["field"]["data"]

    a.get_config("field").data = "内存"
    flush = [data(disk)]
    core_config.lazy_write_config()
    flush.append(data(disk))
    a.write_config()
    flush.append(data(disk))
    a.set_config("field", "显式更新")
    flush.append(data(disk))
    rows["flush"] = flush
    raw = json.loads(disk.read_bytes())
    raw["field"]["data"] = "复制样本"
    original = json.dumps(raw, indent=7, ensure_ascii=False).encode("utf-8") + b"\n"
    disk.write_bytes(original)
    rows["copy"] = [
        m.copy(" a ", " c "),
        (base / "c.json").read_bytes() == original,
        disk.read_bytes() == original,
        m.copy("a", "c"),
        m.copy("missing", "d"),
        m.copy("a", " "),
        m.copy("a", "a"),
        m.copy("", "d"),
        m.copy("a", "d+e"),
    ]
    cached_value = m.get_config("a")["field"].data
    m.drop_cache("a")
    refreshed = m.get_config("a")
    raw["field"]["data"] = "再次更新"
    disk.write_text(json.dumps(raw), encoding="utf8")
    m.clear_cache()
    rows["reload"] = [
        cached_value,
        refreshed["field"].data,
        m.get_config("a") is a,
        a["field"].data,
    ]
    second = ConfigSetManager(root / "other", template(), "_probe")
    rebound = second.get_config("a")
    distinct = ConfigSetManager(root / "extra", template(), "_separate").get_config("a")
    rows["same_name"] = [
        a is rebound,
        a.CONFIG_PATH == root / "other/a.json",
        m.get_config("a") is rebound,
        rebound["field"].data,
        distinct is rebound,
    ]
    rows["delete"] = [
        m.delete("missing"),
        m.delete("c"),
        m.exists("c"),
        m.list_available(),
    ]

    class Nested(ConfigSetManager):
        def _get_config_path(self, config_name):
            return self._base_path / config_name / "settings.json"

        def _list_configs(self):
            return sorted(
                path.parent.name for path in self._base_path.glob("*/settings.json")
            )

    nested = Nested(root / "nested", template())
    rows["subclass"] = [
        nested.create_default("red"),
        nested.copy("red", "blue"),
        list(nested.get_all_configs()),
        nested.exists("blue"),
        nested.delete("red"),
        nested.list_available(),
    ]
    first, second, final = [
        root / f"{name}.json" for name in ("first", "second", "final")
    ]
    for name, path in [("first", first), ("second", second)]:
        StringConfig("seed_" + name, path, template()).set_config("field", name)
    picked = StringConfig("migration", [first, second, final], template())
    rows["migrate_first"] = [
        picked["field"].data,
        first.exists(),
        second.exists(),
        data(final),
    ]
    retained = StringConfig("migration", [second, final], template())
    rows["migrate_existing"] = [
        retained is picked,
        second.exists(),
        data(second),
        data(final),
    ]
    unreadable = root / "unreadable.json"
    unreadable.write_bytes(b"\xff")
    recovered = StringConfig(
        "unreadable", [unreadable, root / "recovered.json"], template()
    )
    rows["migrate_invalid_utf8"] = [
        unreadable.read_bytes().hex(),
        recovered["field"].data,
    ]
    left = StringConfig("from_left", root / "left.json", template("Old left"))
    right = StringConfig("from_right", root / "right.json", template("Old right"))
    left.set_config("field", "左")
    right.set_config("field", "右")
    retained.migrate_from([left, right])
    rows["migrate_from"] = [
        data(final),
        retained["field"].title,
        json.loads(left.CONFIG_PATH.read_bytes()),
        json.loads(right.CONFIG_PATH.read_bytes()),
    ]
    rows["new_jobs"] = sorted({job.id for job in scheduler.get_jobs()} - before)
    return rows


def framework_store():
    import gsuid_core.config as framework

    if not hasattr(framework, "plugin_config_store"):
        return {"available": False}
    store = framework.plugin_config_store
    if not hasattr(framework, "PluginConfigStore"):
        return {
            "available": True,
            "scope": "current profile (see profile_store_contract.py)",
        }
    key = "contract-config-set"
    path = framework.PLUGINS_CONFIGS_PATH / (key + ".json")
    store.set(key, {"sample": 1})
    first = path.exists()
    framework.core_config.lazy_write_config()
    samples = [json.loads(path.read_bytes())["sample"]]
    store.get(key)["sample"] = 2
    store.save_dirty()
    samples.append(json.loads(path.read_bytes())["sample"])
    store.mark_dirty(key)
    store.save_dirty()
    samples.append(json.loads(path.read_bytes())["sample"])
    store.get_all()[key]["sample"] = 3
    store.save_all()
    samples.append(json.loads(path.read_bytes())["sample"])
    legacy = "contract-legacy"
    framework.core_config.config["plugins"] = {
        key: {"sample": "obsolete"},
        legacy: {"sample": 4},
    }
    framework.core_config.write_config()
    saved_before_migration = framework.CONFIG_PATH.read_bytes()
    reloaded = framework.PluginConfigStore()
    migration = {
        "existing": reloaded.get(key)["sample"],
        "new": reloaded.get(legacy)["sample"],
        "backup_matches": framework.CONFIG_PATH.with_name(
            "config_backup.json"
        ).read_bytes()
        == saved_before_migration,
        "old_key_removed": "plugins"
        not in json.loads(framework.CONFIG_PATH.read_bytes()),
    }
    return {
        "available": True,
        "before_flush": first,
        "flushed": samples,
        "legacy_migration": migration,
    }


def child(work, archive):
    def offline(event, args):
        if event in {
            "socket.connect",
            "socket.getaddrinfo",
            "socket.bind",
            "subprocess.Popen",
        }:
            raise PermissionError("offline configuration fixture")

    sys.addaudithook(offline)
    sys.path.insert(0, str(archive or work))
    if archive:
        from plugin_runtime import PluginRuntime
        from sayu_compat import install
        from service_settings import management_scope

        install("probe", "cp", work / "data", source_root=work)
        runtime = PluginRuntime(work / "data")
        runtime.install(work, "probe")
        grant = management_scope(writable=True)
    else:
        grant = nullcontext()
    with grant:
        return {"shared": config_cases(), "framework_store": framework_store()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--child", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--archive", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        args.output.write_text(
            json.dumps(child(args.child, args.archive), ensure_ascii=False, indent=2),
            encoding="utf8",
        )
        return
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        command = [
            sys.executable,
            "-I",
            "-B",
            "-X",
            "utf8",
            str(Path(__file__).resolve()),
            "--child",
            tmp,
            "--output",
            str(args.output.resolve()),
        ]
        if args.reference:
            from native_contract import REFERENCE_COMMIT, copy_reference

            provenance = {
                "reference": REFERENCE_COMMIT,
                **copy_reference(args.reference, work),
            }
        else:
            from build_local import worker_archive

            spec = json.loads((ROOT / "profiles/lock.json").read_bytes())["starrail"]
            blob = worker_archive(spec)
            archive = work / "worker.pyz"
            archive.write_bytes(blob)
            provenance = {"worker_sha256": hashlib.sha256(blob).hexdigest()}
            command += ["--archive", str(archive)]
        env = {**os.environ, "HOME": tmp, "USERPROFILE": tmp}
        subprocess.run(command, cwd=work, env=env, check=True)
    receipt = json.loads(args.output.read_bytes())
    receipt["runtime"] = {**provenance, "python": sys.version.split()[0]}
    args.output.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf8"
    )
    if args.compare:
        reference = json.loads(args.compare.read_bytes())
        differences = sorted(
            k
            for k in reference["shared"] | receipt["shared"]
            if reference["shared"].get(k) != receipt["shared"].get(k)
        )
        if differences:
            raise SystemExit(f"configuration manager differences: {differences}")
        print(f"matched {len(receipt['shared'])} shared configuration observations")


if __name__ == "__main__":
    main()
