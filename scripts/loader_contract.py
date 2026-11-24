"""Exercise the shared Sayu fixture through the packaged worker on two generations."""

import argparse
import asyncio
import importlib
import json
import sys
import tempfile
from pathlib import Path
from types import ModuleType

from build_local import ROOT, archive, digest, worker_archive

from contract_inputs import PHASES, event_for


async def run(core, interpreter):
    sys.path.insert(0, str(core.parent))
    sdk = importlib.import_module(f"{core.name}.yomihime_game_link_sdk")
    sys.path.insert(0, str(ROOT / "packages"))
    sys.modules["sayu_loader._api_v2"] = sdk
    binding = ModuleType("sayu_loader.sayu.runtime")
    sys.modules[binding.__name__] = binding
    fixture = ROOT / "tests" / "contract_fixture"
    with tempfile.TemporaryDirectory(prefix="sayu-contract-") as directory:
        root = Path(directory)
        fixture_files = {
            p.relative_to(fixture).as_posix(): p.read_bytes()
            for p in fixture.rglob("*.py")
        }
        source = archive(fixture_files)
        plugin = "contract_probe"
        worker = worker_archive({"package": plugin, "prefix": "cp"})
        source_file = root / "source.zip"
        source_file.write_bytes(source)
        worker_file = root / "probe.pyz"
        worker_file.write_bytes(worker)
        data = root / "persistent-data"
        binding.WORKERS = {
            plugin: (
                str(interpreter),
                str(worker_file),
                digest(worker),
                str(source_file),
                digest(source),
                str(data),
            )
        }
        module = importlib.import_module("sayu_loader.sayu.module").Module(plugin, None)
        trace = data / "resources" / plugin / "trace.jsonl"
        phases = []
        retired = None
        offset = 0
        prior_identity = None
        for phase_index, commands in enumerate(PHASES):
            await module.start()
            try:
                from types import SimpleNamespace

                actor = SimpleNamespace(actor_id="sample", adapter_id="local")
                identity = module.event(actor)["actor_id"]
                assert identity != actor.actor_id
                assert prior_identity is None or identity == prior_identity
                prior_identity = identity
                overlapping = type(module)(plugin, None)
                try:
                    await overlapping.start()
                except RuntimeError as exc:
                    if "profile_busy" not in str(exc):
                        raise
                else:
                    await overlapping.stop()
                    raise AssertionError("two workers claimed one profile")
                if retired:
                    try:
                        await retired.invoke(None, {"text": "cpstate"})
                    except sdk.ServiceUnavailable:
                        pass
                    else:
                        raise AssertionError(
                            "retired handler entered replacement generation"
                        )
                retired = module.handlers().capabilities["query"]
                report = await module.exchange(
                    {"op": "inventory", "list_triggers": True}
                )
                assert (
                    report["mode"] == "package"
                    and report["registered"]["handlers"] >= 1
                )
                rows = []
                for i, text in enumerate(commands, 1):
                    reply = await module.exchange(
                        {
                            "text": text,
                            "private_image": False,
                            "event": event_for(phase_index, i),
                        }
                    )
                    rows.append(
                        {
                            "input": text,
                            "messages": [
                                part["text"] for part in reply.get("parts", ())
                            ],
                            "status": reply.get("error", "success"),
                        }
                    )
                async with asyncio.timeout(5):
                    while not any(
                        json.loads(line)["label"] == "tick"
                        for line in trace.read_text(encoding="utf-8").splitlines()[
                            offset:
                        ]
                    ):
                        await asyncio.sleep(0.05)
            finally:
                await module.stop()
            before = trace.read_bytes()
            await asyncio.sleep(0.35)
            lines = before.decode("utf-8").splitlines()
            records = [json.loads(line) for line in lines[offset:]]
            offset = len(lines)
            phases.append(
                {
                    "commands": rows,
                    "trace": [r for r in records if r["label"] != "tick"],
                    "ticks": sum(r["label"] == "tick" for r in records),
                    "quiet_after_stop": trace.read_bytes() == before,
                }
            )
        return {
            "fixture_sha256": digest(fixture_files[f"{plugin}/__init__.py"]),
            "worker_sha256": digest(worker),
            "phases": phases,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = asyncio.run(run(args.core.resolve(), args.python.resolve()))
    encoded = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    else:
        print(encoded)
