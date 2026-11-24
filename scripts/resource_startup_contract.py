"""Exercise the SDK module start/stop contract with real, empty resource-only profiles."""

import argparse
import asyncio
import hashlib
import importlib
import json
from pathlib import Path
import sys
import tempfile
from time import monotonic
from types import ModuleType

from build_local import LOCAL, ROOT, worker_archive
from environments import selected


async def exercise(core, serial, observe_seconds=0):
    sys.path[:0] = [str(ROOT / "packages"), str(core.parent)]
    sys.modules["sayu_loader._api_v2"] = importlib.import_module(
        core.name + ".yomihime_game_link_sdk"
    )
    runtime = ModuleType("sayu_loader.sayu.runtime")
    runtime.WORKERS = {}
    sys.modules[runtime.__name__] = runtime
    from sayu_loader.sayu.module import Module

    with tempfile.TemporaryDirectory(prefix="sayu-cold-resources-") as temporary:
        root = Path(temporary)
        locks = json.loads((ROOT / "profiles/lock.json").read_bytes())
        for key, spec in locks.items():
            exe, _ = selected(
                LOCAL / key,
                (ROOT / "profiles" / f"{key}-requirements.txt").read_bytes(),
            )
            source = (
                LOCAL / "runtime" / f"{spec['package']}-{spec['snapshot_sha256']}.zip"
            )
            archive = root / f"{key}.pyz"
            data = worker_archive(spec, public_resources_only=True, snapshot=source)
            archive.write_bytes(data)
            runtime.WORKERS[key] = (
                str(exe),
                str(archive),
                hashlib.sha256(data).hexdigest(),
                str(source),
                spec["snapshot_sha256"],
                str(root / key),
            )

        instances = {key: Module(key, None) for key in locks}
        rows = {}

        async def start(key, module):
            before = monotonic()
            await module.start()
            elapsed = monotonic() - before
            inventory = await module.exchange({"op": "inventory"})
            budget = inventory["public_resource_startup"]
            assert 0 <= budget["spent_seconds"] <= budget["limit_seconds"]
            rows[key] = {
                "worker_sha256": runtime.WORKERS[key][2],
                "startup_seconds": round(elapsed, 3),
                "source_modules": len(inventory["modules"]),
                "registered": inventory["registered"],
                "module_outcomes": inventory["modules"],
                "failed_modules": sum(
                    value is not None for value in inventory["modules"].values()
                ),
                "module_errors": {
                    name: value
                    for name, value in inventory["modules"].items()
                    if value is not None
                },
                "hooks": inventory["failed_hook_phases"],
                "resource_budget": budget,
                "static_httpx": inventory["public_static_httpx"],
                "download_counts": inventory["downloads"]["counts"],
                "downloaded_bytes": inventory["downloads"]["bytes_written"],
                "on_demand": inventory["downloads"]["on_demand"],
                "continuations_at_ready": inventory["downloads"]["continuations"],
            }

        async def stop(key, module):
            proc = module.process
            before = monotonic()
            await module.stop()
            if proc is not None:
                assert proc.returncode == 0
                rows.setdefault(key, {})["stop_seconds"] = round(
                    monotonic() - before, 3
                )

        began = monotonic()
        try:
            # The Core's default setup bound is 30 s. Its host-package
            # registration also restores durable module intents in sequence.
            async with asyncio.timeout(30):
                if serial:
                    for key, module in instances.items():
                        await start(key, module)
                else:
                    async with asyncio.TaskGroup() as tasks:
                        for key, module in instances.items():
                            tasks.create_task(start(key, module))
            total = monotonic() - began
            if observe_seconds:
                await asyncio.sleep(observe_seconds)
                for key, module in instances.items():
                    inventory = await module.exchange({"op": "inventory"})
                    rows[key]["continuations_after_ready"] = inventory["downloads"][
                        "continuations"
                    ]
                    rows[key]["bytes_after_ready"] = inventory["downloads"][
                        "bytes_written"
                    ]
        finally:
            async with asyncio.timeout(5):
                await asyncio.gather(*(stop(k, m) for k, m in instances.items()))
    return {
        "order": "serial" if serial else "concurrent",
        "startup_seconds": round(total, 3),
        "profiles": rows,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--core", type=Path, required=True, help="local Core SDK checkout"
    )
    parser.add_argument(
        "--serial", action="store_true", help="exercise sequential package restore"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--observe-seconds", type=int, choices=range(0, 61), default=0)
    args = parser.parse_args()
    result = asyncio.run(
        exercise(args.core.resolve(), args.serial, args.observe_seconds)
    )
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf8")
