"""Exercise a pinned gsuid_core checkout with an in-memory adapter.

Run with the upstream uv.lock environment. The harness copies gsuid_core into
a fresh working directory because the native data root is relative to its code.
"""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from contract_inputs import PHASES, event_for  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "contract_fixture" / "contract_probe"

REFERENCE_COMMIT = "96cd7353be3100ff982bccc33e554961837c778e"
MANIFEST_SHA256 = "f53bee7020c134de04f6cac0318df78d5a53e8a8e5248c4fe6c2bb12c3ca6c37"


def copy_reference(reference, work):
    # Support a git checkout or the GitHub trees API response for a source archive.
    tree_file = reference.parent / "reference-tree.json"
    if tree_file.exists():
        rows = json.loads(tree_file.read_text("utf-8-sig"))["tree"]
        manifest = {r["path"]: r["sha"] for r in rows if r["type"] == "blob"}
    else:
        tree = subprocess.check_output(
            [
                "git",
                "-C",
                str(reference),
                "ls-tree",
                "-rz",
                REFERENCE_COMMIT,
                "--",
                "gsuid_core",
                "uv.lock",
            ]
        )
        manifest = {}
        for row in tree.decode("utf8").split("\0"):
            if row:
                meta, name = row.split("\t", 1)
                manifest[name] = meta.split()[2]
    manifest = {
        p: h
        for p, h in manifest.items()
        if p.startswith("gsuid_core/") or p == "uv.lock"
    }
    if (
        hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        != MANIFEST_SHA256
    ):
        raise ValueError("reference manifest does not match the pinned upstream tree")
    copied = 0
    for name, expected in manifest.items():
        path = reference / name
        if not path.is_file():
            raise ValueError(f"missing pinned reference blob: {name}")
        data = path.read_bytes()
        header = f"blob {len(data)}\0".encode()
        if hashlib.sha1(header + data).hexdigest() != expected:
            raise ValueError(f"reference blob mismatch: {name}")
        target = work / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        copied += 1
    return {"verified_blobs": copied, "manifest_blobs": len(manifest)}


def normalized(receipt):
    phases = []
    for phase in receipt["phases"]:
        phases.append(
            {
                "commands": [
                    {"input": c["input"], "messages": c["messages"]}
                    for c in phase["commands"]
                ],
                "trace": phase["trace"],
                "timer_fired": phase["ticks"] > 0,
                "quiet_after_stop": phase["quiet_after_stop"],
            }
        )
    return phases


def validate(receipt, *, has_native_master):
    phases = normalized(receipt)
    if len(phases) != 2:
        raise ValueError("missing worker generation")
    expected = [
        [
            "import",
            "start",
            "inspect",
            "first",
            "second",
            "state",
            "failure",
            "state",
            "group",
            "direct",
            *(["admin"] if has_native_master else []),
            "shutdown",
        ],
        ["import", "start", "state", "shutdown"],
    ]
    for i, phase in enumerate(phases):
        if [c["input"] for c in phase["commands"]] != PHASES[i]:
            raise ValueError(f"input sequence differs in generation {i}")
        if [row["label"] for row in phase["trace"]] != expected[i]:
            raise ValueError(f"trace differs in generation {i}")
        if not phase["timer_fired"] or not phase["quiet_after_stop"]:
            raise ValueError(f"timer lifecycle differs in generation {i}")
    messages = [[c["messages"] for c in phase["commands"]] for phase in phases]
    if messages[0][1:] != [
        ["first", "second"],
        ["count 1/1"],
        [],
        [],
        ["count 2/2"],
        [],
        ["group actor2 room"],
        [],
        ["direct actor2"],
        [],
        ["admin owner 0"] if has_native_master else [],
    ] or messages[1] != [["count 1/3"]]:
        raise ValueError("reply, blocking or state behavior differs")
    event = phases[0]["trace"][2].copy()
    event.pop("label")
    if messages[0][0] != [json.dumps(event, sort_keys=True)]:
        raise ValueError("event fields differ between trace and outgoing response")


def offline(event, args):
    if event in {
        "socket.getaddrinfo",
        "socket.connect",
        "socket.bind",
        "subprocess.Popen",
    }:
        raise PermissionError(f"offline fixture blocked {event}")


async def run_phase(work, phase):
    sys.addaudithook(offline)
    sys.path.insert(0, str(work))
    from gsuid_core.utils.database.base_models import init_database

    await init_database()
    from gsuid_core.config import core_config

    core_config.set_config("masters", ["owner"])
    from starlette.websockets import WebSocketState
    from gsuid_core.aps import shutdown_scheduler, start_scheduler
    from gsuid_core.bot import _Bot
    from gsuid_core.handler import decode_inbound_frame, handle_event
    from gsuid_core.utils.database import startup  # noqa: F401  (register migration hooks)
    from gsuid_core.server import (
        GsServer,
        core_start_before_execute,
        core_start_execute,
        core_shutdown_execute,
    )

    class Adapter:
        application_state = WebSocketState.CONNECTED

        def __init__(self):
            self.frames = []

        async def send_bytes(self, data):
            self.frames.append(json.loads(data))

    trace = work / "data" / "contract_probe" / "trace.jsonl"
    reports = work / "data" / "logs" / "error_reports"
    old_reports = set(reports.glob("*.json"))
    previous = len(trace.read_text("utf8").splitlines()) if trace.exists() else 0
    server = GsServer()
    modules = server.load_plugin(work / "gsuid_core" / "plugins" / "contract_probe")
    if not isinstance(modules, list) or len(modules) != 1:
        raise RuntimeError(f"fixture discovery failed: {modules!r}")
    for module in modules:
        server.cached_import(*module)

    await core_start_before_execute()
    await core_start_execute()
    await start_scheduler()
    adapter = Adapter()
    bot = _Bot("contract", adapter)
    bot.start_send_worker()
    process = asyncio.create_task(bot._process())
    commands = []
    try:
        for number, text in enumerate(PHASES[phase], start=1):
            event = event_for(phase, number)
            frame = {
                "bot_id": event["adapter_id"],
                "bot_self_id": "fixture",
                "msg_id": event["message_id"],
                "user_type": event["conversation_kind"],
                "group_id": event["group_id"] or None,
                "user_id": event["actor_id"],
                "user_pm": 6,
                "content": [{"type": "text", "data": text}],
            }
            msg = decode_inbound_frame(json.dumps(frame).encode(), "contract")
            if msg is None:
                raise RuntimeError("native frame decoder rejected fixture")
            await handle_event(bot, msg)
            await asyncio.wait_for(bot.queue.join(), 10)
            await asyncio.wait_for(bot._send_queue.join(), 10)
            parts = []
            for sent in adapter.frames:
                target_id = event["group_id"] or event["actor_id"]
                if (
                    sent["target_type"] != event["conversation_kind"]
                    or sent["target_id"] != target_id
                ):
                    raise ValueError(f"incorrect native destination: {sent!r}")
                content = sent["content"]
                if event["group_id"]:
                    if content[-1] != {"type": "group", "data": target_id}:
                        raise ValueError("native group routing segment missing")
                    content = content[:-1]
                parts.extend(content)
            if any(part["type"] != "text" for part in parts):
                raise ValueError(f"unexpected native outgoing content: {parts!r}")
            messages = [part["data"] for part in parts]
            commands.append({"input": text, "messages": messages})
            adapter.frames.clear()
        await asyncio.sleep(0.55)
    finally:
        process.cancel()
        bot._send_task.cancel()
        await asyncio.gather(process, bot._send_task, return_exceptions=True)
        await shutdown_scheduler()
        await core_shutdown_execute()
    lines = trace.read_text("utf8").splitlines()
    records = [json.loads(line) for line in lines[previous:]]
    controlled_errors = 0
    for path in set(reports.glob("*.json")) - old_reports:
        report = json.loads(path.read_text("utf8"))
        if report.get("event") == "fixture controlled failure":
            controlled_errors += 1
    if controlled_errors != PHASES[phase].count("cpfail"):
        raise ValueError("native exception report does not match the failure stimulus")
    await asyncio.sleep(0.55)
    return {
        "commands": commands,
        "trace": [r for r in records if r["label"] != "tick"],
        "ticks": sum(r["label"] == "tick" for r in records),
        "quiet_after_stop": trace.read_text("utf8").splitlines() == lines,
        "controlled_exception_reports": controlled_errors,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reference", type=Path, default=ROOT / ".sayu-local/reference"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--compare-loader", type=Path, help="compare with a loader receipt"
    )
    parser.add_argument(
        "--phase", type=int, choices=range(len(PHASES)), help=argparse.SUPPRESS
    )
    args = parser.parse_args()
    if args.phase is not None:
        receipt = asyncio.run(run_phase(Path.cwd(), args.phase))
    else:
        base = ROOT / ".sayu-local/reference-env"
        base.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="run-", dir=base))
        sources = copy_reference(args.reference, work)
        plugin = work / "gsuid_core/plugins/contract_probe"
        plugin.mkdir(parents=True)
        source = plugin / "__init__.py"
        shutil.copyfile(FIXTURE / "__init__.py", source)
        phases = []
        for i in range(len(PHASES)):
            output = work / f"phase-{i}.json"
            with (work / f"phase-{i}.log").open("wb") as log:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-X",
                        "utf8",
                        __file__,
                        "--phase",
                        str(i),
                        "--output",
                        str(output),
                    ],
                    cwd=work,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=120,
                )
            if result.returncode:
                raise RuntimeError(f"Phase {i} failed; inspect {work / log.name}")
            phases.append(json.loads(output.read_text("utf8")))
        receipt = {
            "reference_commit": REFERENCE_COMMIT,
            "reference_sources": sources,
            "reference_lock_sha256": hashlib.sha256(
                (work / "uv.lock").read_bytes()
            ).hexdigest(),
            "fixture_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "phases": phases,
        }
    args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), "utf8")
    if args.phase is None:
        validate(receipt, has_native_master=True)
        if args.compare_loader:
            loader = json.loads(args.compare_loader.read_text("utf8"))
            validate(loader, has_native_master=False)
            if loader.get("fixture_sha256") != receipt["fixture_sha256"]:
                raise ValueError("native and loader fixture hashes differ")
            native = normalized(receipt)
            if native[0]["trace"][-2] != {"label": "admin", "actor": "owner", "pm": 0}:
                raise ValueError("native master grant did not reach the expected actor")
            if loader["phases"][0]["commands"][-1].get("status") != "no_match":
                raise ValueError(
                    "loader accepted a user without a trusted admin identity"
                )
            # Both outcomes were asserted above. Compare every remaining observation.
            native[0]["commands"][-1]["messages"] = []
            native[0]["trace"].pop(-2)
            if native != normalized(loader):
                raise ValueError("native and loader receipts differ")
            print(
                "Shared event, routing, denial and lifecycle outcomes agree. "
                "Native granted its configured master; Loader denied the unverified identity."
            )


if __name__ == "__main__":
    main()
