"""Offline index, nested download, update and failure probes for the public mirror port."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from native_contract import REFERENCE_COMMIT, copy_reference, offline  # noqa: E402


def index(entries):
    rows = [
        '<html>\n<head><title>Index of /</title></head>\n<body>\n<h1>Index of /</h1><hr><pre><a href="../">../</a>\n'
    ]
    for name, size in entries:
        rows.append(
            f'<a href="{name}">{name}</a>  21-Nov-2026 17:28                   {size}\n'
        )
    return ("".join(rows) + "</pre><hr></body>\n</html>").encode()


async def observe(mode):
    import httpx2

    work = Path.cwd()
    sys.addaudithook(offline)
    base = "https://mirror.example.test"
    prefix = "/Fixture/assets/"
    calls = []
    files = {"item.json": b'{"test":1}', "sub/entry.json": b"[1,2]"}
    missing = False

    def sender(request):
        path = request.url.path.removeprefix(prefix)
        calls.append(path)
        if phase == "connection_failed":
            raise httpx2.ConnectError("fixture disconnected", request=request)
        if phase == "index_missing":
            return httpx2.Response(404)
        if phase == "non_index":
            return httpx2.Response(
                200, content=b"<html>missing directory listing</html>"
            )
        if path == "":
            content = index([("item.json", len(files["item.json"])), ("sub/", "-")])
        elif path == "sub/":
            content = index([("entry.json", len(files["sub/entry.json"]))])
        elif missing:
            return httpx2.Response(404)
        else:
            content = files[path]
        return httpx2.Response(200, content=content)

    transport = httpx2.MockTransport(sender)
    if mode == "native":
        sys.path.insert(0, str(work))
        from gsuid_core.utils.download_resource.download_core import download_all_file

        class Client(httpx2.AsyncClient):
            def __init__(self, *a, **kw):
                super().__init__(*a, transport=transport, **kw)

        httpx2.AsyncClient = Client
    else:
        sys.path.insert(0, str(ROOT / "worker"))
        from plugin_downloads import Downloads

        port = Downloads(
            work, plugin="Fixture", bulk_mirrors=(base,), transport=transport
        )
        download_all_file = port.mirror.download_all_file
    cache = work / "private"
    report = {}

    def digest():
        return {
            f.relative_to(cache).as_posix(): hashlib.sha256(f.read_bytes()).hexdigest()
            for f in cache.rglob("*")
            if f.is_file()
        }

    for phase in (
        "first",
        "same_size",
        "changed",
        "missing",
        "connection_failed",
        "index_missing",
        "non_index",
    ):
        calls.clear()
        if phase == "same_size":
            files["item.json"] = b'{"test":2}'
        if phase == "changed":
            files["sub/entry.json"] = b"[1,2,3]"
        if phase == "missing":
            missing = True
            files["item.json"] = b'{"test":123}'
        error = None
        try:
            assert (
                await download_all_file("Fixture", {"assets": cache}, URL=base) is None
            )
        except Exception as exc:
            error = type(exc).__name__
        report[phase] = {"calls": sorted(calls), "files": digest(), "error": error}
    if mode == "loader":
        evidence = port.mirror.diagnostics()
        assert evidence["counts"]["downloaded"] == 3
        assert {e["reason"] for e in evidence["failures"]} == {
            "http_error",
            "unreachable",
            "no_index",
        }
        assert all(len(e["item_sha256"]) == 64 for e in evidence["failures"])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("loader", "native"))
    parser.add_argument(
        "--reference", type=Path, default=ROOT / ".sayu-local/reference"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.mode:
        report = asyncio.run(observe(args.mode))
    else:
        report = {"reference": REFERENCE_COMMIT}
        with tempfile.TemporaryDirectory(prefix="sayu-resources-") as temp:
            for mode in ("native", "loader"):
                work = Path(temp) / mode
                work.mkdir()
                if mode == "native":
                    copy_reference(args.reference, work)
                output = work / "observed.json"
                subprocess.run(
                    [
                        sys.executable,
                        "-IB",
                        "-X",
                        "utf8",
                        __file__,
                        "--mode",
                        mode,
                        "--output",
                        str(output),
                    ],
                    cwd=work,
                    check=True,
                    timeout=60,
                )
                report[mode] = json.loads(output.read_bytes())
        reference = report["native"]
        candidate = report["loader"]
        for phase in reference:
            assert (
                reference[phase] == candidate[phase]
                and candidate[phase]["error"] is None
            ), phase
        assert reference["first"]["calls"] == [
            "",
            "item.json",
            "sub/",
            "sub/entry.json",
        ]
        assert reference["same_size"]["calls"] == ["", "sub/"]
        assert reference["changed"]["calls"] == ["", "sub/", "sub/entry.json"]
        assert (
            reference["missing"]["files"]
            == candidate["missing"]["files"]
            == reference["changed"]["files"]
        )
        for phase in ("connection_failed", "index_missing", "non_index"):
            assert (
                reference[phase]["calls"] == [""] and reference[phase]["error"] is None
            )
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf8")


if __name__ == "__main__":
    main()
