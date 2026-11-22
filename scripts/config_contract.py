"""Compare the same plugin settings transaction in isolated native and Loader processes."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile


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
        else:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
            from sayu_compat import install

            install("probe", "cp", args.child / "data", source_root=args.child)
        args.output.write_text(
            json.dumps(probe(), indent=2, ensure_ascii=False), encoding="utf8"
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
        subprocess.run(command, cwd=root, check=True)


if __name__ == "__main__":
    main()
