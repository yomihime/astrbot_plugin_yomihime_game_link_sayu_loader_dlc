"""Compare the same plugin settings transaction in isolated native and Loader processes."""

import argparse
from contextlib import nullcontext
import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import get_args

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def schema_cases():
    import msgspec
    from gsuid_core.data_store import get_res_path
    from gsuid_core.utils.plugins_config import models as s
    from gsuid_core.utils.plugins_config.gs_config import StringConfig, all_config_list

    p = get_res_path("probe/config") / "all.json"
    state = s.ButtonState()
    status = s.ButtonStatusField("ok", "Status")
    a = ("title", "description")
    leaf = {"label": s.GsStrConfig(*a, "start"), "div": s.GsDivider(*a)}
    inner = s.GsRepeatGroupConfig(*a, [], leaf)
    entries = [
        s.GsStrConfig(*a, "abc", regex="a.*", secret=True),
        s.GsBoolConfig(*a, False),
        s.GsDictConfig(*a, {"row": [7]}),
        s.GsListStrConfig(*a, ["x"]),
        s.GsListConfig(*a, [3]),
        s.GsIntConfig(*a, 4, options=[3, 4]),
        s.GsFloatConfig(*a, 6.5),
        s.GsImageConfig(*a, "image", "assets", "logo"),
        s.GsTimeRConfig(*a, (2, 3)),
        s.GsDivider(*a),
        s.GsFileUploadConfig(*a, "page", "assets", "page"),
        s.GsFilesUploadConfig(*a, "assets"),
        s.GsDateConfig(*a, datetime.date(2024, 2, 29)),
        s.GsTimeRangeConfig(*a, ((0, 15), (6, 25))),
        s.GsColorConfig(*a, "#ff0000"),
        s.GsButtonConfig(*a, state, "/api/test", "Run", status_fields=[status]),
        s.GsRepeatGroupConfig(*a, [], {"group": inner}),
        s.GsTimeConfig(*a, "legacy"),
    ]
    values = {type(item).__name__: item for item in entries}
    assert set(values) == {t.__name__ for t in get_args(s.GSC)}
    config = StringConfig("typed config", p, values)
    encoded = msgspec.json.encode(values)
    roundtrip = msgspec.json.decode(encoded, type=dict[str, s.GSC])
    assert config.get_raw_config() == msgspec.to_builtins(roundtrip)
    writes = [
        config.set_config("GsFloatConfig", 12),
        config.set_config("GsFloatConfig", True),
        config.set_config("GsDateConfig", "2024-02-30"),
        config.set_config("GsDateConfig", "2026-03-08"),
        config.set_config("GsTimeRConfig", [12, 18]),
        config.set_config("GsTimeRangeConfig", [[3, "04"], [5, 6]]),
        config.set_config("GsTimeRangeConfig", [[], None]),
        config.set_config("GsButtonConfig", "ignored"),
        config.set_button_state("GsIntConfig", state),
        config.set_button_state(
            "GsButtonConfig", s.ButtonState("done", 100, "ok", {"ok": "yes"})
        ),
        config.set_config(
            "GsRepeatGroupConfig",
            [None, {"group": [{"label": "child", "div": "ignored"}]}],
        ),
    ]
    untyped = config.get_raw_config()
    untyped["GsDateConfig"]["data"] = "not-a-date"
    untyped["broken"] = {"type": "external"}
    untyped["orphan"] = msgspec.to_builtins(s.GsStrConfig(*a, "keep"))
    p.write_text(json.dumps(untyped), encoding="utf8")
    values["GsIntConfig"].options = [4, 5]
    values["GsButtonConfig"].label = "Again"
    recovered = StringConfig("typed config", p, values)
    group = recovered["GsRepeatGroupConfig"].data
    assert group[0]["group"].data[0]["label"].data == "child"
    previous = StringConfig(
        "earlier",
        p.with_name("old.json"),
        {"GsStrConfig": s.GsStrConfig(*a, "old value")},
    )
    recovered.migrate_from(previous)
    assert (
        recovered["GsStrConfig"].data == "old value" and not previous.get_raw_config()
    )
    assert all_config_list["typed config"] is recovered
    return {
        "tagged_types": list(values),
        "wire_sha256": hashlib.sha256(encoded).hexdigest(),
        "writes": writes,
        "restored": recovered.get_raw_config(),
        "persisted_sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
        "state_view": s.button_state_view(recovered["GsButtonConfig"].data),
    }


def probe():
    from gsuid_core.data_store import get_res_path
    from gsuid_core.utils.plugins_config.gs_config import StringConfig
    from gsuid_core.utils.plugins_config.models import (
        GsBoolConfig,
        GsIntConfig,
        GsStrConfig,
    )

    path = get_res_path("probe/config") / "values.json"
    defaults = {
        "flag": GsBoolConfig("Flag", "", False),
        "count": GsIntConfig("Number", "", 2),
        "name": GsStrConfig("Name", "", "A"),
    }
    config = StringConfig("probe", path, defaults)
    initial = config.get_raw_config()
    writes = [
        config.set_config("flag", True),
        config.set_config("count", True),
        config.set_config("name", "B"),
        config.set_config("absent", 5),
    ]
    moved = path.with_name("new.json")
    reopened = StringConfig("probe", [path, moved], defaults)
    return {
        "initial": initial,
        "writes": writes,
        "same": reopened is config,
        "raw": reopened.get_raw_config(),
        "disk": json.loads(moved.read_bytes()),
        "migrated": not path.exists(),
        "default": reopened.get_config("absent").data,
        "keys": list(reopened),
        "length": len(reopened),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--child", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--archive", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--compare",
        type=Path,
        help="Compare the resulting JSON to another backend receipt",
    )
    args = parser.parse_args()
    if args.child is not None:

        def offline(event, _args):
            if event in {
                "socket.connect",
                "socket.getaddrinfo",
                "socket.bind",
                "subprocess.Popen",
            }:
                raise PermissionError("offline settings fixture")

        sys.addaudithook(offline)
        if args.reference:
            sys.path.insert(0, str(args.child))
            grant = nullcontext()
        else:
            sys.path.insert(0, str(args.archive))
            from sayu_compat import install
            from service_settings import management_scope

            install("probe", "cp", args.child / "data", source_root=args.child)
            grant = management_scope(writable=True)
        with grant:
            report = {**probe(), "schema_cases": schema_cases()}
        if not args.reference:
            from port_errors import Unsupported
            from gsuid_core.utils.plugins_config.gs_config import all_config_list

            settings = all_config_list["typed config"]
            settings.get_config("GsStrConfig").data = "foreign"
            assert settings["GsStrConfig"].data == "old value"
            try:
                settings.set_config("GsStrConfig", "foreign")
            except Unsupported:
                pass
            else:
                raise AssertionError("ungranted settings mutation")
        args.output.write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf8"
        )
        return
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        command = [
            sys.executable,
            "-I",
            "-B",
            "-X",
            "utf8",
            str(Path(__file__).resolve()),
            "--child",
            temp,
            "--output",
            str(args.output.resolve()),
        ]
        if args.reference:
            from native_contract import copy_reference

            copy_reference(args.reference, root)
            command += ["--reference", temp]
        else:
            from build_local import worker_archive

            archive = root / "worker.pyz"
            lock = json.loads((ROOT / "profiles/lock.json").read_bytes())["starrail"]
            archive.write_bytes(worker_archive(lock))
            command += ["--archive", str(archive)]
        subprocess.run(command, cwd=root, check=True)
    if args.compare and json.loads(args.compare.read_bytes()) != json.loads(
        args.output.read_bytes()
    ):
        raise SystemExit("native and Loader settings differ")


if __name__ == "__main__":
    main()
