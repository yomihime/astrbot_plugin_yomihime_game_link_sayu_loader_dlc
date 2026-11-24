"""Compare GsServer module discovery and execution with the private package loader."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
from types import ModuleType

sys.path.insert(0, str(Path(__file__).resolve().parent))
from native_contract import ROOT, REFERENCE_COMMIT, copy_reference, offline


def observe_facade(registry, owner):
    api_name = f"plugins.{owner}.{owner}.api"
    api = ModuleType(api_name)
    sys.modules[api_name] = api
    try:
        bound = registry.bind_meta_plugin_facade("draw", owner, [api_name])
        snapshot = registry.list_meta_plugins()
        result = [
            bound,
            snapshot.copy(),
            registry.provides_of(owner),
            registry.require("draw") is api,
            registry.import_api(owner) is api,
        ]
        snapshot.clear()
        result.append(registry.list_meta_plugins())
        registry.unregister_meta_plugin(owner)
        result.extend([registry.list_meta_plugins(), owner + ".api" in sys.modules])
        try:
            registry.require("draw")
        except registry.MetaPluginNotFoundError as exc:
            result.extend([exc.provides, str(exc)])
        else:
            raise AssertionError("retired provider still resolves")
        return result
    finally:
        sys.modules.pop(api_name, None)


def compare():
    sys.addaudithook(offline)
    root = Path.cwd()
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(ROOT / "worker"))
    from gsuid_core.server import GsServer
    from gsuid_core import meta_plugins, server as native_server
    from plugin_meta import install_meta
    from plugin_runtime import load_entry, module_entries

    source = root / "plugins"
    source.mkdir(exist_ok=True)
    cases = {
        "Standalone": ["Standalone.py"],
        "Single": ["Single/__init__.py"],
        "Full": [
            "Full/__full__.py",
            "Full/__init__.py",
            "Full/z/__init__.py",
            "Full/a/__init__.py",
        ],
        "Nested": [
            "Nested/__nest__.py",
            "Nested/Nested/__init__.py",
            "Nested/Nested/mod/__init__.py",
        ],
        "Implicit": [
            "Implicit/Implicit/__init__.py",
            "Implicit/Implicit/sub/__init__.py",
        ],
        "NoInit": ["NoInit/__full__.py", "NoInit/sub/__init__.py"],
    }
    server = GsServer()
    result = {}
    trace = root / "import-order"
    for name, files in cases.items():
        for file in files:
            target = source / file
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                "with open('import-order', 'a') as f: f.write(__name__ + '\\n')\n",
                encoding="utf8",
            )
        target = source / (files[0] if name == "Standalone" else name)
        reference = server.load_plugin(target)
        local = module_entries(source, name)
        assert local == [(key, path) for key, path, _ in reference]
        trace.write_text("", encoding="utf8")
        for key, path, kind in reference:
            server.cached_import(key, path, kind)
            server.cached_import(key, path, kind)
        observed = trace.read_text().splitlines()
        for key, _, _ in reference:
            sys.modules.pop(key, None)
        trace.write_text("", encoding="utf8")
        for key, path in local:
            load_entry(key, path)
            load_entry(key, path)
        assert observed == trace.read_text().splitlines() == [key for key, _ in local]
        result[name] = observed
    server_port, registry = ModuleType("server"), ModuleType("meta_plugins")
    install_meta(registry, server_port, "FixtureMeta")
    fixture = source / "FixtureMeta"
    fixture.mkdir()
    observations = []
    for marker, config in (
        (False, ""),
        (True, ""),
        (False, '[tool.gsuid]\nkind = "meta"\nprovides = " draw "'),
        (True, "malformed = ["),
        (False, "malformed = ["),
    ):
        flag = fixture / "__meta_plugin__.py"
        if marker:
            flag.touch()
        else:
            flag.unlink(missing_ok=True)
        (fixture / "pyproject.toml").write_text(config, encoding="utf8")
        expected = native_server.read_meta_plugin_info(fixture)
        actual = server_port.read_meta_plugin_info(fixture)
        fields = asdict(expected) if expected else None
        assert fields == (asdict(actual) if actual else None)
        observations.append(fields)
    native_observed = observe_facade(meta_plugins, "FixtureMeta")
    assert observe_facade(registry, "FixtureMeta") == native_observed
    result["meta"] = {"discovery": observations, "facade": native_observed}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        args.output.write_text(json.dumps(compare(), indent=2), encoding="utf8")
        return
    with tempfile.TemporaryDirectory(prefix="sayu-layout-") as directory:
        work = Path(directory)
        copy_reference(args.reference.resolve(), work)
        env = {
            k: v
            for k, v in os.environ.items()
            if k.upper() in {"SYSTEMROOT", "WINDIR", "LANG"}
        }
        env.update(TMP=directory, TEMP=directory, TMPDIR=directory)
        out = work / "observations.json"
        subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-X",
                "utf8",
                str(Path(__file__).resolve()),
                "--child",
                "--output",
                str(out),
            ],
            check=True,
            cwd=work,
            env=env,
        )
        result = {
            "reference": REFERENCE_COMMIT,
            "layouts": json.loads(out.read_bytes()),
        }
        args.output.write_text(json.dumps(result, indent=2), encoding="utf8")


if __name__ == "__main__":
    main()
