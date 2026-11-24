"""Compare plugin FastAPI routes and static mounts without opening a socket."""

import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
from io import BytesIO
import subprocess
import sys
import tempfile
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build_local import worker_archive  # noqa: E402
from native_contract import REFERENCE_COMMIT, copy_reference, offline  # noqa: E402


async def status_and_qr_cases():
    from PIL import Image
    from gsuid_core.data_store import get_res_path
    from gsuid_core.status.plugin_status import plugins_status, register_status
    from gsuid_core.utils.boardcast.models import BoardCastMsgDict
    from gsuid_core.utils.cookie_manager.qrlogin import get_qrcode_base64

    async def initial():
        return "first"

    async def final():
        return 42

    icon = Image.new("RGBA", (2, 3))
    register_status(icon, "fixture", {"value": initial})
    assert await plugins_status["fixture"]["status"]["value"]() == "first"
    register_status(icon, "fixture", {"value": final})
    result = {
        "status": await plugins_status["fixture"]["status"]["value"](),
        "icon": list(plugins_status["fixture"]["icon"].size),
        "message_fields": sorted(BoardCastMsgDict.__required_keys__),
    }
    for adapter in ("astrbot", "onebot", "onebot_v12"):
        path = get_res_path("probe") / (adapter + ".png")
        value = await get_qrcode_base64(
            "https://fixture.invalid/qr/demo", path, adapter
        )
        content = (
            base64.b64decode(value.removeprefix("base64://"))
            if isinstance(value, str)
            else value
        )
        with Image.open(BytesIO(content)) as image:
            result[adapter] = [
                type(value).__name__,
                list(image.size),
                hashlib.sha256(content).hexdigest(),
                path.exists(),
            ]
        if path.exists():
            assert path.read_bytes() == content
    return result


async def published_image_case():
    from gsuid_core.utils.image.utils import sget
    from port_errors import Unsupported

    url = "https://image.example.org/public/cover.png"
    address = ("93.184.216.34", 443)
    tls = MagicMock()
    connection = MagicMock()
    response = connection.getresponse.return_value
    response.status = 200
    response.getheader.return_value = "identity"
    response.getheaders.return_value = [("Content-Type", "image/png")]
    response.read.return_value = b"public fixture"
    import socket

    peer = MagicMock()
    sockets = MagicMock()
    sockets.SOCK_STREAM = socket.SOCK_STREAM
    sockets.getaddrinfo.return_value = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", address)
    ]
    sockets.socket.return_value = peer
    with (
        patch("plugin_public_http.socket", sockets),
        patch("plugin_public_http.ssl.create_default_context", return_value=tls),
        patch(
            "plugin_public_http.http.client.HTTPSConnection", return_value=connection
        ),
    ):
        reply = await sget(url)
        assert reply.content == b"public fixture"
        peer.connect.assert_called_once_with(address)
        tls.wrap_socket.assert_called_once_with(
            peer, server_hostname="image.example.org"
        )
        connection.request.assert_called_once_with(
            "GET",
            "/public/cover.png",
            headers={"Accept": "image/*", "Accept-Encoding": "identity"},
        )
        try:
            await sget("https://image.example.org/unlisted.png")
        except Unsupported:
            pass
        else:
            raise AssertionError("unpublished source requested a network resource")
        assert sockets.getaddrinfo.call_count == 1


async def image_fetch_cases(mode):
    import httpx2
    from gsuid_core.data_store import get_res_path
    from gsuid_core.utils.image import utils

    calls = []

    def respond(request):
        path = request.url.path
        calls.append(path)
        if path == "/redirect":
            return httpx2.Response(302, headers={"Location": "/image"})
        if path == "/missing":
            return httpx2.Response(404, content=b"missing")
        if path == "/failure":
            raise httpx2.ReadTimeout("fixture", request=request)
        return httpx2.Response(
            200, headers={"X-Fixture": "image"}, content=b"controlled"
        )

    transport = httpx2.MockTransport(respond)
    original = httpx2.AsyncClient
    if mode == "native":
        utils.httpx2.AsyncClient = lambda **kw: original(
            transport=transport, trust_env=False, **kw
        )
    else:
        from plugin_image_fetch import install_image_fetch
        from port_errors import Unsupported
        from sayu_compat import package

        try:
            await utils.sget("https://fixture.invalid/unbound")
        except Unsupported:
            pass
        else:
            raise AssertionError("unbound image fetch succeeded")
        utils = install_image_fetch(package, Path.cwd() / "data", transport=transport)
    observed = []
    try:
        url = "https://fixture.invalid/redirect"
        for i in range(3):
            if i == 2:
                cached = get_res_path(["IMAGE_CACHE", "sget"]) / (
                    hashlib.sha256(url.encode()).hexdigest() + ".bin"
                )
                os.utime(cached, (1, 1))
            resp = await utils.sget(url, use_cache=True)
            observed.append(
                [
                    resp.content.hex(),
                    str(resp.url),
                    resp.status_code,
                    resp.headers.get("X-Fixture"),
                    [item.status_code for item in resp.history],
                ]
            )
        for path in ("missing", "failure"):
            try:
                await utils.sget("https://fixture.invalid/" + path)
            except httpx2.HTTPError as exc:
                observed.append(type(exc).__name__)
            else:
                raise AssertionError("expected HTTP error")
        assert calls == [
            "/redirect",
            "/image",
            "/redirect",
            "/image",
            "/missing",
            "/failure",
        ]
        return {"responses": observed, "requests": calls}
    finally:
        httpx2.AsyncClient = original


async def exercise(mode, archive):
    sys.addaudithook(offline)
    if mode == "native":
        sys.path.insert(0, str(Path.cwd()))
    else:
        sys.path.insert(0, str(archive))
        from sayu_compat import install

        source = Path.cwd() / "source" / "probe"
        source.mkdir(parents=True)
        (source / "example.py").write_text(
            'COVER = "https://image.example.org/public/cover.png"\n', encoding="utf8"
        )
        install("probe", "", Path.cwd() / "data")
        await published_image_case()
    from gsuid_core.app_life import app
    from gsuid_core.web_app import app as alias
    from fastapi import APIRouter
    from fastapi.staticfiles import StaticFiles
    from httpx import AsyncClient, ASGITransport

    assert alias is app

    class PluginStatic(StaticFiles):
        async def get_response(self, path, scope):
            response = await super().get_response(path, scope)
            response.headers["Access-Control-Allow-Origin"] = "*"
            return response

    folder = Path.cwd() / "sample-assets"
    folder.mkdir()
    (folder / "font.css").write_bytes(b"body{color:#102030}" * 44)
    router = APIRouter(prefix="/probe")

    @router.get("/double/{number}")
    async def double(number: int):
        return {"doubled": number * 2}

    app.include_router(router)
    app.mount("/probe/assets", PluginStatic(directory=folder), name="fixture-assets")
    results = {}
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://fixture.invalid",
        trust_env=False,
    ) as client:
        for method, suffix in (
            ("GET", "double/5"),
            ("GET", "double/x"),
            ("POST", "double/5"),
            ("GET", "assets/font.css"),
            ("HEAD", "assets/font.css"),
            ("GET", "assets/missing"),
        ):
            response = await client.request(
                method, "/probe/" + suffix, headers={"Accept-Encoding": "gzip"}
            )
            results[method + " " + suffix] = [
                response.status_code,
                hashlib.sha256(response.content).hexdigest(),
                {
                    k: response.headers[k]
                    for k in (
                        "content-type",
                        "content-encoding",
                        "access-control-allow-origin",
                    )
                    if k in response.headers
                },
            ]
    assert results["GET double/5"][0] == 200 and results["GET assets/missing"][0] == 404
    results["image_fetch"] = await image_fetch_cases(mode)
    results["status_and_qr"] = await status_and_qr_cases()
    if mode == "loader":
        from sayu_compat import install

        install("next_probe", "", Path.cwd() / "next_data")
        from gsuid_core.app_life import app as fresh

        assert fresh is not app and all(
            getattr(r, "path", "") != "/probe/assets" for r in fresh.routes
        )
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", choices=("loader", "native"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--loader-python", type=Path, default=Path(sys.executable))
    parser.add_argument("--archive", type=Path)
    parser.add_argument(
        "--reference", type=Path, default=ROOT / ".sayu-local/reference"
    )
    args = parser.parse_args()
    if args.child:
        report = asyncio.run(exercise(args.child, args.archive))
    else:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            native = root / "native"
            loader = root / "loader"
            sources = copy_reference(args.reference.resolve(), native)
            loader.mkdir()
            worker = root / "worker.pyz"
            worker.write_bytes(
                worker_archive(
                    json.loads((ROOT / "profiles/lock.json").read_bytes())["waves"]
                )
            )
            report = {
                "reference": REFERENCE_COMMIT,
                "sources": sources,
                "worker_sha256": hashlib.sha256(worker.read_bytes()).hexdigest(),
            }
            for kind, exe in (
                ("native", sys.executable),
                ("loader", args.loader_python.resolve()),
            ):
                dest = root / (kind + ".json")
                subprocess.run(
                    [
                        str(exe),
                        "-IB",
                        "-X",
                        "utf8",
                        __file__,
                        "--child",
                        kind,
                        "--output",
                        str(dest),
                        "--archive",
                        str(worker),
                    ],
                    cwd=root / kind,
                    check=True,
                    timeout=90,
                )
                report[kind] = json.loads(dest.read_bytes())
    args.output.write_text(json.dumps(report, indent=2), encoding="utf8")
    if not args.child and report["native"] != report["loader"]:
        raise SystemExit("native and Loader ASGI observations differ")


if __name__ == "__main__":
    main()
