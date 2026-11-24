"""Compare the adapter registry against GsServer using two in-memory connections."""

import argparse
import asyncio
import json
import logging
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

from native_contract import ROOT, REFERENCE_COMMIT, copy_reference, offline


class LocalAdapter:
    def __init__(self):
        self.frames = []

    async def accept(self):
        pass

    async def send_text(self, text):
        self.frames.append(text)

    async def close(self):
        pass


async def exercise(server, module, attach):
    trace = []
    assert module.GsServer() is server
    assert not server.active_bot and not server.active_ws

    def connected():
        trace.append("stale")

    server.on_bot_connect(connected)

    def replacement():
        trace.append("sync")

    replacement.__name__ = connected.__name__
    module.GsServer.on_bot_connect(replacement)

    @server.on_bot_connect
    async def async_hook():
        trace.append("async")

    a, b = LocalAdapter(), LocalAdapter()
    await attach(a, "adapter-a")
    await attach(b, "adapter-b")
    await server.send("only-a", "adapter-a")
    await server.send("unroutable", "missing")
    await server.broadcast("all")
    return {
        "bots": sorted(server.active_bot),
        "sockets": sorted(server.active_ws),
        "callbacks": trace,
        "frames": [a.frames, b.frames],
    }, (a, b)


async def compare(work):
    sys.addaudithook(offline)
    sys.path.insert(0, str(work))
    from gsuid_core import server as native_module
    from gsuid_core.gss import gss

    native, sockets = await exercise(gss, native_module, gss.connect)
    for key, sock in zip(native["bots"], sockets, strict=True):
        await gss.disconnect(key, sock)
    sys.path.insert(0, str(ROOT / "worker"))
    from plugin_gss import install_gss

    fixture = SimpleNamespace()
    registry = install_gss(lambda name: SimpleNamespace(), fixture)

    async def attach(sock, key):
        await registry.attach(key, SimpleNamespace(bot_id=key), sock)

    worker, sockets = await exercise(registry.server, fixture, attach)
    if (
        native != worker
        or worker["frames"] != [["only-a", "all"], ["all"]]
        or worker["callbacks"] != ["sync", "async"] * 2
    ):
        raise ValueError((native, worker))
    for key, sock in zip(worker["bots"], sockets, strict=True):
        registry.detach(key, LocalAdapter())
        assert registry.server.active_ws.get(key) is sock
        registry.detach(key, sock)
    return {
        "reference": REFERENCE_COMMIT,
        "connected": native,
        "after_detach": {
            "native_retained_bots": sorted(gss.active_bot),
            "loader_bots": sorted(registry.server.active_bot),
            "native_sockets": sorted(gss.active_ws),
            "loader_sockets": sorted(registry.server.active_ws),
        },
    }


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf8")
    sys.stderr.reconfigure(encoding="utf8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reference", type=Path, default=ROOT / ".sayu-local/reference"
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="gss-registry-") as tmp:
        work = Path(tmp)
        copy_reference(args.reference, work)
        try:
            report = asyncio.run(compare(work))
        finally:
            logging.shutdown()
            sys.excepthook = sys.__excepthook__
    if args.output:
        args.output.write_text(json.dumps(report, indent=2), encoding="utf8")
    print(json.dumps(report, ensure_ascii=True))
