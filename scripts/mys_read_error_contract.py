"""Run identical, offline record-GET failures through the original and packaged Bot."""

import argparse
import asyncio
from contextlib import ExitStack
import errno
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_local import worker_archive  # noqa: E402
from mys_account_contract import COOKIE, UID  # noqa: E402
from native_contract import ROOT, REFERENCE_COMMIT, copy_reference, offline  # noqa: E402


BAD = b"private-upstream-body-not-for-chat"
OK = b'{"retcode":0,"data":{"ok":true}}'
CONNECT = "[mys_request] 请求连接错误..."
INVALID = "[mys_request] 请求错误, 请联系Bot主人检查控制台! 错误信息: Expecting value: line 1 column 1 (char 0)"


class Exchange:
    def reset(self, faults):
        self.faults = list(faults)
        self.order = []

    def attempt(self):
        action = self.faults.pop(0)
        self.order.append("GET")
        return action

    def native_transport(self):
        from aiohttp import ClientConnectorError, TCPConnector

        async def open_connection(connector, req, traces, timeout):
            action = self.attempt()
            assert req.method == "GET" and req.headers["Cookie"] == COOKIE
            if action == "refused":
                raise ClientConnectorError(req.connection_key, ConnectionRefusedError())
            payload = {"invalid": BAD, "blank": b" \t "}.get(action, OK)
            reply = (
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
                + str(len(payload)).encode()
                + b"\r\n\r\n"
                + payload
            )
            loop = asyncio.get_running_loop()
            proto = connector._factory()

            class Wire(asyncio.Transport):
                closed = False

                def is_closing(self):
                    return self.closed

                def write(self, data):
                    if b"\r\n\r\n" in data:
                        loop.call_soon(proto.data_received, reply)

                def close(self):
                    if not self.closed:
                        self.closed = True
                        loop.call_soon(proto.connection_lost, None)

                abort = close

            proto.connection_made(Wire())
            return proto

        return patch.object(TCPConnector, "_create_connection", open_connection)

    def loader_transport(self):
        import httpx2

        class BrokenBody(httpx2.AsyncByteStream):
            async def __aiter__(self):
                yield b"{"
                raise httpx2.ReadError(COOKIE)

        async def reply(req):
            action = self.attempt()
            assert req.method == "GET" and req.headers["Cookie"] == COOKIE
            if action == "refused":
                raise httpx2.ConnectError(COOKIE) from ConnectionRefusedError()
            if action == "connect_unknown":
                raise httpx2.ConnectError(COOKIE)
            if action == "permission":
                raise PermissionError(COOKIE)
            if action == "wrapped_permission":
                raise httpx2.ConnectError(COOKIE) from PermissionError(
                    errno.EACCES, COOKIE
                )
            if action == "timeout":
                raise httpx2.ConnectTimeout(COOKIE)
            if action == "cancel":
                raise asyncio.CancelledError()
            if action == "partial":
                return httpx2.Response(200, stream=BrokenBody())
            if action == "large":
                return httpx2.Response(200, content=b"a" * (8 * 1024 * 1024 + 1))
            headers = {"Content-Type": "application/json"}
            if action in ("206", "403", "429"):
                return httpx2.Response(int(action), content=BAD, headers=headers)
            if action == "html":
                return httpx2.Response(
                    200, content=BAD, headers={"Content-Type": "text/html"}
                )
            return httpx2.Response(
                200,
                content={"invalid": BAD, "blank": b" \t "}.get(action, OK),
                headers=headers,
            )

        original = httpx2.AsyncClient
        return patch.object(
            httpx2,
            "AsyncClient",
            lambda **kw: original(transport=httpx2.MockTransport(reply), **kw),
        )

    async def target_send(self, text, kind, target, *args, **kwargs):
        assert (kind, target) == ("direct", "actor")
        self.order.append(text)


async def run(mode, worker):
    sys.addaudithook(offline)
    native = mode == "native"
    exchange = Exchange()
    with ExitStack() as stack:
        if native:
            sys.path.insert(0, str(Path.cwd()))
            from gsuid_core.utils.database import base_models as db

            await db.init_database()
            from gsuid_core.utils.database.startup import ensure_core_database_tables
            from gsuid_core.utils.api.mys.api import DAILY_NOTE
            from gsuid_core.utils.api.mys_api import _MysApi

            await ensure_core_database_tables()
            api, endpoint = _MysApi(), DAILY_NOTE
            stack.enter_context(exchange.native_transport())
        else:
            sys.path.insert(0, str(worker))
            import plugin_mys as mys
            from plugin_bindings import binding_scope
            from plugin_runtime import PluginRuntime
            from sayu_compat import install
            from service_settings import management_scope

            data = Path.cwd() / "private"
            install("probe", "", data, source_root=Path.cwd())
            runtime = PluginRuntime(data)
            runtime.install(Path.cwd(), "probe")
            with management_scope(writable=True):
                async with runtime.orm.startup():
                    await runtime.orm.migrate()
            stack.enter_context(
                binding_scope("actor", "adapter", True, account_records=True)
            )
            stack.enter_context(exchange.loader_transport())
            api, endpoint = mys.MysApi(), mys.ENDPOINTS["DAILY_NOTE"]
        from gsuid_core.bot import Bot
        from gsuid_core.models import Event
        from gsuid_core.utils.database.models import GsUser

        await GsUser.insert_data(
            user_id="actor",
            bot_id="adapter",
            mys_id="700000077",
            uid=UID,
            region="cn_gf01",
            cookie=COOKIE,
            device_id="fixture",
            fp="fixture",
        )

        async def read():
            return await api.endpoint_request(
                endpoint, UID, params={"role_id": UID}, ds_mode="none"
            )

        async def plugin_callback(bot, ev):
            return await read()

        class Outbox(list):
            def append(self, part):
                super().append(part)
                exchange.order.append(part["text"])

        event = Event(user_id="actor", bot_id="adapter", user_type="direct")
        target = Bot(exchange if native else None, event)
        if not native:
            target.messages = Outbox()
        shared = {}
        for name, faults, expected_result, order in (
            (
                "reconnect",
                ["refused", "ok"],
                {"retcode": 0, "data": {"ok": True}},
                ["GET", CONNECT, "GET"],
            ),
            (
                "invalid_then_ok",
                ["invalid", "ok"],
                {"retcode": 0, "data": {"ok": True}},
                ["GET", INVALID, "GET"],
            ),
            ("exhaust", ["refused", "invalid"], -999, ["GET", CONNECT, "GET", INVALID]),
            ("next_invocation", ["ok"], {"retcode": 0, "data": {"ok": True}}, ["GET"]),
        ):
            exchange.reset(faults)
            value = await plugin_callback(target, event)
            assert value == expected_result and exchange.order == order, (
                name,
                value,
                exchange.order,
            )
            shared[name] = {"result": value, "order": exchange.order}
        for fault in ("refused", "invalid"):
            exchange.reset([fault])
            try:
                await read()
            except ValueError as exc:
                assert exchange.order == ["GET"] and "Bot" in str(exc)
                shared[f"no_bot_{fault}"] = {"error": str(exc), "order": exchange.order}
            else:
                raise AssertionError("missing Bot was silently accepted")
        # The native logger/parser may fail on None for a blank JSON body.
        # Retain both observable exceptions; neither response is replayed.
        exchange.reset(["blank"])
        try:
            await plugin_callback(target, event)
        except Exception as exc:
            assert exchange.order == ["GET"]
            blank = {"error": type(exc).__name__, "order": exchange.order}
        else:
            raise AssertionError("unexpected success for whitespace-only JSON")
        denied = {}
        if native:
            await db.engine.dispose()
        else:
            for fault in (
                "403",
                "429",
                "206",
                "html",
                "permission",
                "wrapped_permission",
                "connect_unknown",
                "timeout",
                "cancel",
                "partial",
                "large",
            ):
                exchange.reset([fault])
                try:
                    await plugin_callback(target, event)
                except (Exception, asyncio.CancelledError) as exc:
                    assert (
                        exchange.order == ["GET"]
                        and COOKIE not in str(exc)
                        and "https://" not in str(exc)
                    )
                    denied[fault] = type(exc).__name__
                else:
                    raise AssertionError("unconfirmed response treated as success")
            exchange.reset([])
            with binding_scope("other", "adapter", True, account_records=True):
                assert (
                    await plugin_callback(target, event) == -51 and not exchange.order
                )
            denied["other_actor"] = "zero HTTP"
        return {"common": shared, "guards": denied, "blank": blank}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--child", choices=("loader", "native"))
    parser.add_argument("--worker", type=Path)
    args = parser.parse_args()
    if args.child:
        report = asyncio.run(run(args.child, args.worker))
    else:
        with tempfile.TemporaryDirectory(prefix="mys-read-") as temp:
            work = Path(temp)
            checked = copy_reference(ROOT / ".sayu-local/reference", work / "native")
            (work / "loader").mkdir()
            worker = args.worker.resolve() if args.worker else work / "worker.pyz"
            if args.worker is None:
                worker.write_bytes(
                    worker_archive(
                        json.loads((ROOT / "profiles/lock.json").read_bytes())["genshin"]
                    )
                )
            report = {
                "reference": REFERENCE_COMMIT,
                "verified": checked,
                "worker_sha256": hashlib.sha256(worker.read_bytes()).hexdigest(),
            }
            for mode in ("native", "loader"):
                output = work / f"{mode}.json"
                subprocess.run(
                    [
                        sys.executable,
                        "-IB",
                        "-X",
                        "utf8",
                        __file__,
                        "--child",
                        mode,
                        "--worker",
                        str(worker),
                        "--output",
                        str(output),
                    ],
                    cwd=work / mode,
                    check=True,
                    timeout=60,
                )
                report[mode] = json.loads(output.read_bytes())
            assert report["native"]["common"] == report["loader"]["common"]
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf8")


if __name__ == "__main__":
    main()
