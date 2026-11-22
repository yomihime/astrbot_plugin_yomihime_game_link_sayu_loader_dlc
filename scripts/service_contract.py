"""Compare registered routes, activation and persistence over two isolated processes."""

import argparse
import asyncio
from contextlib import nullcontext
import hashlib
import importlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/service_fixture/service_probe"
NAME = FIXTURE.name


async def probe(mode, phase, work):
    sys.path.insert(0, str(ROOT / "scripts"))
    from native_contract import offline

    sys.addaudithook(offline)
    if mode == "native":
        sys.path.insert(0, str(work))
        from gsuid_core.utils.database.base_models import init_database

        await init_database()
        from gsuid_core.server import GsServer
        from gsuid_core.handler import _sv_authorized
        from gsuid_core.config import plugin_config_store

        manage = nullcontext
        server = GsServer()
        for declaration in server.load_plugin(work / "gsuid_core/plugins" / NAME):
            server.cached_import(*declaration)
    else:
        sys.path.insert(0, str(ROOT / "worker"))
        import sayu_compat as loader
        from service_settings import management_scope
        from functools import partial

        manage = partial(management_scope, writable=True)
        from plugin_runtime import PluginRuntime

        source = work / "plugins"
        loader.install(
            NAME, "sp", work / "data", source_root=source, runtime_mode="package"
        )
        PluginRuntime(work / "data").import_package(source, NAME)

    plugin = importlib.import_module(f"plugins.{NAME}")

    async def snapshot():
        allowed = []
        registered = []
        for sv, text in [
            (plugin.query, "spquery"),
            (plugin.restricted, "sprestricted"),
        ]:
            if mode == "native":
                ev = SimpleNamespace(
                    raw_text=text,
                    is_tome=False,
                    group_id="room",
                    user_id="actor",
                    user_type="group",
                    bot_self_id="adapter",
                )
                routes = list(sv.TL["fullmatch"].values())
                registered.append(len(routes))
                allowed.append(
                    _sv_authorized(sv, ev, 6)
                    and any(route.check_command(ev) for route in routes)
                )
            else:
                routes = [
                    r for r in loader.registered_triggers() if r["service"] == sv.name
                ]
                registered.append(len(routes))
                try:
                    await loader.dispatch(
                        text,
                        actor_id="actor",
                        group_id="room",
                        conversation_kind="group",
                    )
                except loader.NoMatch:
                    allowed.append(False)
                else:
                    allowed.append(True)
        return {
            "registered": registered,
            "allowed": allowed,
            "priority": plugin.query.priority,
            "plugin_enabled": plugin.plugin.enabled,
            "service_enabled": plugin.query.enabled,
        }

    result = [await snapshot()]
    if phase == 1:
        with manage():
            plugin.activate()
        result.append(await snapshot())
        with manage():
            plugin.close_plugin()
        result.append(await snapshot())
    else:
        with manage():
            plugin.close_service()
        result.append(await snapshot())
    if mode == "native":
        plugin_config_store.save_dirty()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--mode", choices=["native", "loader"], help=argparse.SUPPRESS)
    parser.add_argument("--phase", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.mode:
        report = asyncio.run(probe(args.mode, args.phase, Path.cwd()))
    else:
        from native_contract import copy_reference, REFERENCE_COMMIT

        report = {
            "reference": REFERENCE_COMMIT,
            "fixture_sha256": hashlib.sha256(
                (FIXTURE / "__init__.py").read_bytes()
            ).hexdigest(),
        }
        with tempfile.TemporaryDirectory(prefix="sayu-service-probe-") as tmp:
            for mode in ("native", "loader"):
                work = Path(tmp) / mode
                work.mkdir()
                source = work / "plugins"
                if mode == "native":
                    copy_reference(args.reference.resolve(), work)
                    source = work / "gsuid_core/plugins"
                shutil.copytree(FIXTURE, source / NAME)
                phases = []
                for phase in (1, 2):
                    path = work / f"phase-{phase}.json"
                    subprocess.run(
                        [
                            sys.executable,
                            "-I",
                            "-B",
                            "-X",
                            "utf8",
                            __file__,
                            "--output",
                            str(path),
                            "--mode",
                            mode,
                            "--phase",
                            str(phase),
                        ],
                        cwd=work,
                        check=True,
                        timeout=60,
                    )
                    phases.append(json.loads(path.read_bytes()))
                report[mode] = phases
        assert report["native"] == report["loader"], report
        stages = [row for phase in report["loader"] for row in phase]
        assert all(
            row["registered"] == [1, 1] and not row["allowed"][1] for row in stages
        )
        assert [row["allowed"][0] for row in stages] == [
            False,
            True,
            False,
            False,
            False,
        ]
        assert [row["priority"] for row in stages] == [5, 2, 2, 2, 3]
        assert stages[2] == stages[3]
    args.output.write_text(json.dumps(report, indent=2), encoding="utf8")


if __name__ == "__main__":
    main()
