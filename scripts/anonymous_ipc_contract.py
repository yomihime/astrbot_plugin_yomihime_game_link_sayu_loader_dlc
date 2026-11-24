"""Exercise an exact DLC archive's normal worker IPC with an offline SDK source."""

import argparse
import asyncio
from hashlib import sha256
from importlib import import_module
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from zipfile import ZipFile


def worker_main(archive, source, data):
    """Run the sealed worker unchanged. Fence its broker and replace the remote Kuro POST."""
    import aiohttp

    sys.path.insert(0, str(archive))
    launch = subprocess.Popen

    def fence(arguments, **kwargs):
        if arguments[:3] != [sys.executable, "-IB", "-c"] or arguments[4:] != [
            str(archive)
        ]:
            raise PermissionError("unexpected child in offline fixture")
        prelude = "import sys;sys.path.insert(0,sys.argv[1]);from worker_storage import install_offline_guard;install_offline_guard();"
        return launch(
            [*arguments[:3], prelude + arguments[3], *arguments[4:]], **kwargs
        )

    class Rejected:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def json(self):
            return {"code": 220, "msg": "fixture token rejected"}

    async def reject(self, method, url, **kwargs):
        if method != "POST" or not str(url).startswith("https://api.kurobbs.com/"):
            raise AssertionError("unexpected operation in test issuer")
        return Rejected()

    sys.argv = [str(archive), str(source), str(data)]
    with (
        patch.object(subprocess, "Popen", fence),
        patch.object(aiohttp.ClientSession, "_request", reject),
    ):
        runpy.run_path(str(archive), run_name="__main__")


async def check(package, digest, core):
    from anonymous_account_fixture import OfflineReads

    if sha256(package.read_bytes()).hexdigest() != digest:
        raise ValueError("package digest mismatch")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with ZipFile(package) as z:
            rows = json.loads(z.read("release.json"))["files"]
            for row in rows:
                path = root / row["path"]
                assert path.resolve().is_relative_to(root) and not path.exists()
                data = z.read(row["path"])
                assert (
                    sha256(data).hexdigest() == row["sha256"]
                    and len(data) == row["size"]
                )
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        hits = []
        fixture = OfflineReads(core, root, hits)
        adapter = import_module("sayu_loader.sayu.module")
        original = adapter.WORKERS["waves"]
        adapter.WORKERS["waves"] = (*original[:-1], str(root / "data"))
        sdk = fixture.sdk
        deadline = asyncio.get_running_loop().time() + 60
        context = sdk.InvocationView(
            "anonymous-ipc-contract",
            "command",
            "fixture-owner",
            "fixture-private",
            "sayuww",
            1,
            1,
            deadline=deadline,
            adapter_id="fixture-adapter",
            capability_id="account",
            conversation_kind="direct",
        )
        active = True

        def require_active():
            if not active:
                raise sdk.AccessDenied()

        async def check_active():
            require_active()

        bound = SimpleNamespace(
            invocation=context,
            http=fixture.bound(fixture.http, require_active, check_active),
        )

        async def bind(value):
            require_active()
            assert value is context
            return bound

        module = adapter.Module(
            "waves", SimpleNamespace(scopes=SimpleNamespace(bind=bind))
        )
        spawn = asyncio.create_subprocess_exec

        async def offline_child(exe, isolated, no_pyc, archive, source, data, **kwargs):
            assert [isolated, no_pyc] == ["-I", "-B"] and Path(
                archive
            ).name == "worker.pyz"
            return await spawn(
                exe,
                isolated,
                no_pyc,
                str(Path(__file__).resolve()),
                "--child",
                archive,
                source,
                data,
                **kwargs,
            )

        with patch.object(asyncio, "create_subprocess_exec", offline_child):
            await module.start()
            try:
                request = {
                    "text": "ww添加token TEST-REJECTED",
                    "event": module.event(context, True),
                    "capability_id": "account",
                    "private_image": True,
                }
                response = await module.exchange(request, owner_context=context)
                assert "parts" in response, (response.get("error"), hits)
                assert [part.get("text") for part in response["parts"]] == [
                    "fixture token rejected"
                ]
                assert hits == [["GET", "event.kurobbs.com", "/event/ip"]]
                active = False
                try:
                    await module.exchange(request, owner_context=context)
                except sdk.AccessDenied:
                    pass
                else:
                    raise AssertionError("revoked invocation started another exchange")
                assert len(hits) == 1
            finally:
                active = False
                await module.stop()
        return {
            "package": digest,
            "worker": original[2],
            "snapshot": original[4],
            "sdk_attempts": hits,
            "revocation_denied": True,
            "account_result": "fixture rejected token",
        }


def main():
    if sys.argv[1:2] == ["--child"]:
        worker_main(*(Path(arg).resolve() for arg in sys.argv[2:]))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--package-sha256", required=True)
    parser.add_argument("--core", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = asyncio.run(
        check(args.package.resolve(), args.package_sha256, args.core.resolve())
    )
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
