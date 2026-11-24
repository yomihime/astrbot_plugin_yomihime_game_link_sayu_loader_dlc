"""Exercise real account commands from locked packages with synthetic identities.

Run once per profile using --mode native and --mode loader, each with --output.
For native use the verified reference virtualenv. Run --mode compare with
--native-result and --loader-result pointing to those JSON receipts.
Pass --worker and --worker-sha256 to run archived bytes. Both sides run their
real startup and shutdown hooks. After startup, pause the scheduler before
the first event so timer jobs cannot race this account-only test.
For Waves, --mode policy compares the frozen and offline Loader receipts
against the same native result and asserts the exact request-field differences.
"""

import argparse
import asyncio
import base64
from contextlib import asynccontextmanager, closing, ExitStack, nullcontext
import hashlib
import importlib
import json
import logging
import os
from pathlib import Path
import runpy
import sqlite3
import subprocess
import sys
import tempfile
import traceback
from unittest.mock import patch
from urllib.parse import urlsplit
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_local import ROOT, LOCAL, worker_archive
from environments import selected
from native_contract import REFERENCE_COMMIT, copy_reference, offline
import mys_package_session_fixture as mys_session

LOCK = json.loads((ROOT / "profiles/lock.json").read_bytes())
UID_A, UID_B = "100000071", "100000082"
SYNTHETIC = "TEST-ONLY-ACCOUNT-FIXTURE"
DEVICE = "0" * 32


def scenarios(key):
    prefix = LOCK[key]["prefix"]
    bind, switch, delete = (
        ("绑定", "切换", "删除")
        if key == "waves"
        else ("绑定UID", "切换UID", "解绑UID")
    )
    query = {"waves": "查看", "genshin": "便笺", "starrail": "绑定信息"}[key]
    cases = [
        (name, actor, prefix + text)
        for name, actor, text in (
            ("empty", "A", query),
            ("bad_format", "A", bind + " wrong"),
            ("bind", "A", bind + " " + UID_A),
            ("duplicate", "A", bind + " " + UID_A),
            ("other_query", "B", query),
            ("bound_query", "A", query),
            ("other_remove", "B", delete + " " + UID_A),
            ("second", "A", bind + " " + UID_B),
            ("switch_second", "A", switch + " " + UID_B),
            ("switch", "A", switch + " " + UID_A),
            ("remove", "A", delete + " " + UID_A),
            ("remove_last", "A", delete + " " + UID_B),
            ("final", "A", query),
        )
    ]
    if key == "waves":
        cases.extend(
            (label, actor, prefix + text)
            for label, actor, text in [
                ("credential_rejected", "A", "添加token TEST-REJECTED"),
                ("credential_add", "A", f"添加token {SYNTHETIC},{DEVICE}"),
                ("credential_other", "B", "获取token"),
                ("credential_other_adapter", "A", "获取token"),
                ("credential_other_remove", "B", "删除token " + UID_A),
                ("credential_refresh", "A", "刷新绑定"),
                ("credential_get", "A", "获取token"),
                ("credential_remove", "A", "删除token " + UID_A),
                ("credential_final", "A", "获取token"),
            ]
        )
    return cases


def snapshot(databases, key):
    """Read committed rows on separate SQLite connections. Only emit fixture facts."""
    bindings, credentials = {}, []
    column = "sr_uid" if key == "starrail" else "uid"
    for path in databases:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            tables = {
                t.lower()
                for (t,) in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            for table in ("wavesbind",) if key == "waves" else ("gsbind", "bindings"):
                if table in tables:
                    for actor, value in db.execute(
                        f"SELECT user_id,{column} FROM {table} WHERE bot_id='onebot' AND user_id IN ('A','B')"
                    ):
                        if value:
                            bindings[actor] = value.split("_")
            if "wavesuser" in tables:
                for actor, uid, cookie, did in db.execute(
                    "SELECT user_id,uid,cookie,did FROM wavesuser WHERE bot_id='onebot' AND user_id IN ('A','B')"
                ):
                    credentials.append([actor, uid, cookie == SYNTHETIC, did == DEVICE])
            if key != "waves" and "gsuser" in tables:
                for actor, uid, cookie in db.execute(
                    f"SELECT user_id,{column},cookie FROM gsuser WHERE bot_id='onebot' AND user_id IN ('A','B')"
                ):
                    credentials.append(
                        [actor, uid, bool(cookie and SYNTHETIC in cookie)]
                    )
    return {"bindings": bindings, "credential_rows": sorted(credentials)}


def compact(kind, data):
    if kind == "node":
        return {"node": [compact(p["type"], p["data"]) for p in data]}
    if kind == "text":
        return {
            "text": data.replace(SYNTHETIC, "<fixture-token>").replace(
                DEVICE, "<fixture-device>"
            )
        }
    if kind == "image" and isinstance(data, str):
        raw = base64.b64decode(data.removeprefix("base64://"))
        return {"image_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    return {"segment": kind}


@asynccontextmanager
async def native(work, package, libraries, lifecycle, include_core_login=False):
    sys.path[:0] = [str(work), str(work / "gsuid_core")]
    sys.path.append(str(libraries))
    from gsuid_core.utils.database import base_models as db

    await db.init_database()
    from gsuid_core.utils.database import startup  # noqa: F401 -- registers migration hooks
    from gsuid_core import aps
    from gsuid_core.gss import gss
    from gsuid_core import server
    from gsuid_core.bot import _Bot
    from gsuid_core.handler import decode_inbound_frame, handle_event
    from gsuid_core.sv import SL
    from starlette.websockets import WebSocketState

    server.refresh_installed_dependencies()
    gss.is_load = True
    if include_core_login:
        importlib.import_module("gsuid_core.buildin_plugins.core_command.user_login")
    gss._import_module_list(gss.load_plugin(work / "gsuid_core/plugins" / package))
    lifecycle["hooks"] = {
        name: sorted(h.func.__module__ + "." + h.func.__name__ for h in hooks)
        for name, hooks in (
            ("start_before", server.core_start_before_def),
            ("start", server.core_start_def),
            ("shutdown", server.core_shutdown_def),
        )
    }

    class Capture(logging.Handler):
        def emit(self, record):
            exc = record.exc_info[1] if record.exc_info else sys.exception()
            lifecycle["logged_errors"].append(
                [lifecycle["phase"], type(exc).__name__ if exc else record.levelname]
            )

    capture = Capture(logging.ERROR)
    logging.getLogger().addHandler(capture)
    aps.remove_repeat_job()
    for name, execute in (
        ("start_before", server.core_start_before_execute),
        ("start", server.core_start_execute),
    ):
        lifecycle["phase"] = name
        await execute()
        lifecycle["finished"].append(name)
    await aps.start_scheduler()
    lifecycle["scheduler_started"] = aps.scheduler.running
    aps.scheduler.pause()
    lifecycle["phase"] = "dispatch"
    handlers = sorted(
        {
            t.func.__module__ + "." + t.func.__name__
            for s in SL.lst.values()
            for routes in s.TL.values()
            for t in routes.values()
            if t.func.__module__.startswith("plugins.")
        }
    )

    class Adapter:
        application_state = WebSocketState.CONNECTED

        def __init__(self):
            self.parts = []

        async def send_bytes(self, data):
            self.parts.extend(
                compact(p["type"], p["data"]) for p in json.loads(data)["content"]
            )

    conn = Adapter()
    bot = _Bot("fixture", conn)
    bot.start_send_worker()
    process = asyncio.create_task(bot._process())

    async def send(actor, text, n, adapter):
        conn.parts.clear()
        frame = {
            "bot_id": adapter,
            "bot_self_id": "fixture",
            "msg_id": str(n),
            "user_id": actor,
            "user_pm": 6,
            "user_type": "direct",
            "content": [{"type": "text", "data": text}],
        }
        await handle_event(
            bot, decode_inbound_frame(json.dumps(frame).encode(), "fixture")
        )
        await asyncio.wait_for(bot.queue.join(), 20)
        await asyncio.wait_for(bot._send_queue.join(), 20)
        return conn.parts.copy()

    try:
        yield send, handlers
    finally:
        process.cancel()
        bot._send_task.cancel()
        await asyncio.gather(process, bot._send_task, return_exceptions=True)
        try:
            lifecycle["phase"] = "shutdown"
            await aps.shutdown_scheduler()
            await asyncio.sleep(0)
            await server.core_shutdown_execute()
            lifecycle["finished"].append("shutdown")
            lifecycle["scheduler_stopped"] = not aps.scheduler.running
        finally:
            logging.getLogger().removeHandler(capture)
            await db.engine.dispose()


@asynccontextmanager
async def loader(work, key, lifecycle):
    archive = work / "worker.pyz"
    sys.path.insert(0, str(archive))
    entry = runpy.run_path(str(archive), run_name="worker_account_fixture")
    if entry["PACKAGE"] != LOCK[key]["package"] or entry["RUNTIME_MODE"] != "package":
        raise ValueError("supplied worker does not implement the selected package")
    worker = entry["Worker"](work / "plugins", work / "data")
    api = entry["sayu_compat"]
    lifecycle["phase"] = "Worker.start"
    await worker.start()
    lifecycle["finished"].append("Worker.start")
    runtime = worker.plugin
    lifecycle["hooks"] = {
        name: sorted(h.func.__module__ + "." + h.func.__name__ for h in hooks)
        for name, hooks in (
            ("start_before", runtime.before_hooks),
            ("start", runtime.start_hooks),
            ("shutdown", runtime.shutdown_hooks),
        )
    }
    lifecycle["scheduler_started"] = runtime.scheduler.running
    runtime.scheduler.pause()
    if worker.reads is not None:
        worker.reads.start()
    lifecycle["phase"] = "dispatch"
    handlers = sorted({r["handler"] for r in api.registered_triggers()})

    async def send(
        actor, text, n, adapter, *, account=True, owner=True, private=True, group=False
    ):
        request = {
            "text": text,
            "capability_id": "account" if account else "query",
            "owner_action": owner,
            "private_image": private,
            "event": {
                "actor_id": actor,
                "adapter_id": adapter,
                "group_id": "fixture-group" if group else "",
                "message_id": str(n),
                "conversation_kind": "group" if group else "direct",
                "conversation_id": "fixture-group" if group else actor,
            },
        }
        reply = await worker.handle(request)
        return [compact(k, v) for p in reply["parts"] for k, v in p.items()]

    try:
        yield send, handlers
    finally:
        lifecycle["phase"] = "shutdown"
        from resource_broker import CLIENT

        try:
            if worker.reads is not None:
                worker.reads.close()
            await runtime.stop()
            lifecycle["finished"].append("PluginRuntime.stop")
            lifecycle["scheduler_stopped"] = not runtime.scheduler.running
            lifecycle["failed_hook_phases"] = dict(runtime.hook_errors)
            lifecycle["failed_modules"] = {
                name: err for name, err in runtime.modules.items() if err
            }
        finally:
            if CLIENT is not None:
                CLIENT.close()
            if worker.reads is not None:
                await asyncio.to_thread(worker.reads.join)


def install_loader_io(stack, work):
    """Enter the candidate's resource policy; the broker child only sees offline origins."""
    archive = work / "worker.pyz"
    sys.path.insert(0, str(archive))
    import game_profile
    import resource_broker
    from worker_storage import install_offline_guard, install_write_guard

    if getattr(game_profile, "PUBLIC_RESOURCES_ONLY", False):
        from resource_httpx import static_httpx

        launch = subprocess.Popen

        def fenced(argv, **kwargs):
            if argv[:3] != [sys.executable, "-IB", "-c"] or argv[4:] != [str(archive)]:
                raise ValueError("unexpected resource child")
            # Execute the frozen broker normally, installing its socket guard
            # before the first request. Only this bootstrap subprocess is allowed.
            prefix = (
                "import sys;sys.path.insert(0,sys.argv[1]);"
                "from worker_storage import install_offline_guard;install_offline_guard();"
            )
            return launch([*argv[:3], prefix + argv[3], *argv[4:]], **kwargs)

        with patch.object(subprocess, "Popen", fenced):
            stack.enter_context(resource_broker.activate(archive))
        stack.enter_context(static_httpx(game_profile, resource_broker.CLIENT))
    install_write_guard(work, work / "data")
    install_offline_guard()
    return bool(resource_broker.CLIENT)


async def child(
    key, mode, work, libraries, stack, lifecycle, include_core_login=False, stored=False
):
    if mode == "loader":
        lifecycle["resource_broker"] = install_loader_io(stack, work)
    else:
        storage = runpy.run_path(str(ROOT / "worker/worker_storage.py"))
        storage["install_write_guard"](work, work / "data")

    def deny_network(event, args):
        if event in (
            "socket.sendto",
            "socket.sendmsg",
            "socket.gethostbyname",
            "socket.gethostbyaddr",
            "socket.getnameinfo",
        ):
            raise PermissionError("offline account fixture")
        offline(event, args)

    sys.addaudithook(deny_network)
    import httpx
    import httpx2

    hits = []
    http_details = []
    startup_http = []
    original_send = httpx.AsyncClient.send

    def fingerprint(value):
        value = str(value)
        if not value:
            return "<empty>"
        for name, marker in (
            ("accepted-token", SYNTHETIC),
            ("rejected-token", "TEST-REJECTED"),
            ("secondary", "fixture-secondary"),
            ("device", DEVICE),
        ):
            if value == marker:
                return "<fixture-" + name + ">"
        if "192.0.2.1" in value:
            return "<fixture-ip>:" + hashlib.sha256(value.encode()).hexdigest()
        if "127.127.127.127" in value:
            return "<upstream-default-ip>:" + hashlib.sha256(value.encode()).hexdigest()
        return hashlib.sha256(value.encode()).hexdigest()

    def call_stack(frames):
        names = {
            "requests.py",
            "request_util.py",
            "util.py",
            "deal.py",
            "resource_httpx.py",
            "__init__.py",
        }
        return [
            [Path(f.filename).name, f.name, f.lineno]
            for f in frames
            if Path(f.filename).name in names
        ][-10:]

    async def observe_send(self, request, **kwargs):
        if request.url.host not in (
            "event.kurobbs.com",
            "api.ipify.org",
            "httpbin.org",
        ):
            return await original_send(self, request, **kwargs)
        row = {
            "client": "httpx",
            "endpoint": [request.method, str(request.url)],
            "headers": {k: fingerprint(v) for k, v in request.headers.items()},
            "payload_bytes": len(request.content),
            "path": call_stack(traceback.extract_stack()),
        }
        http_details.append(row)
        try:
            answer = await original_send(self, request, **kwargs)
        except Exception as exc:
            row.update(
                error=type(exc).__name__,
                rejected_at=call_stack(traceback.extract_tb(exc.__traceback__)),
            )
            raise
        row["status"] = answer.status_code
        return answer

    def offline_httpx2(request):
        if include_core_login:
            payload = mys_session.reply(
                request.method, request.url, request.headers, key, http_details
            )
            if payload is not None:
                return httpx2.Response(200, json=payload, request=request)
        target = [request.method, request.url.host, request.url.path]
        if lifecycle["phase"] == "dispatch":
            hits.append(target)
        elif len(startup_http) < 32:
            startup_http.append(target)
        return httpx2.Response(
            503, json={"message": "offline resource origin"}, request=request
        )

    async def httpx2_async(self, request):
        return offline_httpx2(request)

    def httpx2_sync(self, request):
        return offline_httpx2(request)

    async def external(self, request):
        if include_core_login:
            payload = mys_session.reply(
                request.method, request.url, request.headers, key, http_details
            )
            if payload is not None:
                return httpx.Response(200, json=payload, request=request)
        hits.append([request.method, request.url.host, request.url.path])
        if str(request.url) == "https://event.kurobbs.com/event/ip":
            return httpx.Response(200, text="192.0.2.1", request=request)
        return httpx.Response(
            503, json={"retcode": -1, "message": "offline fixture"}, request=request
        )

    class Response:
        def __init__(self, payload):
            self.payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self):
            return self.payload

    async def kuro_transport(self, method, url, **kwargs):
        target = urlsplit(str(url))
        data = kwargs.get("data") or {}
        headers = kwargs.get("headers", {})
        if include_core_login:
            payload = mys_session.reply(method, url, headers, key, http_details)
            return Response(
                payload
                if payload is not None
                else {"retcode": -1, "message": "offline origin"}
            )
        token = headers.get("token")
        secondary = headers.get("b-at") == "fixture-secondary"
        if target.hostname == "api.kurobbs.com":
            http_details.append(
                {
                    "client": "aiohttp",
                    "endpoint": [method, target.path],
                    "headers": {k.lower(): fingerprint(v) for k, v in headers.items()},
                    "form": {k: fingerprint(v) for k, v in data.items()},
                    "path": call_stack(traceback.extract_stack()),
                }
            )
        hits.append(
            [
                method,
                target.hostname,
                target.path,
                data.get("gameId"),
                token == SYNTHETIC,
                secondary,
            ]
        )
        if target.hostname != "api.kurobbs.com" or method != "POST":
            return Response({"code": 400, "msg": "fixture rejected destination"})
        if token != SYNTHETIC and not secondary:
            return Response({"code": 220, "msg": "fixture token rejected"})
        if target.path == "/gamer/role/list":
            roles = (
                [
                    {
                        "id": 1,
                        "userId": 2,
                        "gameId": 3,
                        "serverId": "test-server",
                        "roleId": UID_A,
                        "roleName": "测试旅人",
                    }
                ]
                if str(data["gameId"]) == "3"
                else []
            )
            return Response({"code": 200, "data": roles})
        if target.path == "/aki/roleBox/requestToken":
            return Response({"code": 200, "data": {"accessToken": "fixture-secondary"}})
        if target.path in ("/user/login/log", "/aki/roleBox/akiBox/refreshData"):
            return Response({"code": 200, "data": {}})
        return Response({"code": 400, "msg": "fixture has no panel data"})

    backend = (
        loader(work, key, lifecycle)
        if mode == "loader"
        else native(
            work, LOCK[key]["package"], libraries, lifecycle, include_core_login
        )
    )
    with ExitStack() as stack:
        stack.enter_context(patch.object(httpx.AsyncClient, "send", observe_send))
        stack.enter_context(
            patch.object(
                httpx2.AsyncHTTPTransport, "handle_async_request", httpx2_async
            )
        )
        stack.enter_context(
            patch.object(httpx2.HTTPTransport, "handle_request", httpx2_sync)
        )
        stack.enter_context(
            patch.object(httpx.AsyncHTTPTransport, "handle_async_request", external)
        )
        if key == "waves" or include_core_login:
            import aiohttp

            stack.enter_context(
                patch.object(aiohttp.ClientSession, "_request", kuro_transport)
            )
        async with backend as (send, handlers):
            rows = []
            denied = {}
            inputs = (
                mys_session.scenarios(key, stored=stored)
                if include_core_login
                else scenarios(key)
            )
            for i, (label, actor, text) in enumerate(inputs):
                hits.clear()
                http_details.clear()
                calls = []
                adapter = "alt" if label == "credential_other_adapter" else "onebot"
                try:
                    with (
                        mys_session.handler_trace(calls)
                        if include_core_login
                        else nullcontext()
                    ):
                        value = {
                            "parts": await asyncio.wait_for(
                                send(actor, text, i, adapter), 25
                            )
                        }
                except Exception as e:
                    value = {
                        "error": type(e).__name__,
                        "detail": str(e)[:250],
                        "stack": [
                            [Path(f.filename).name, f.name, f.lineno]
                            for f in traceback.extract_tb(e.__traceback__)[-6:]
                        ],
                    }
                rows.append(
                    {
                        "case": label,
                        "actor": actor,
                        "adapter": adapter,
                        "input": "core添加 <synthetic credential>"
                        if include_core_login and label == "cookie"
                        else text.replace(SYNTHETIC, "<fixture-token>").replace(
                            DEVICE, "<fixture-device>"
                        ),
                        **value,
                        **({"executed_handlers": calls} if include_core_login else {}),
                        "external_http": hits.copy(),
                        "http_details": http_details.copy(),
                        "state": snapshot(
                            [
                                p
                                for p in (work / "data").rglob("*")
                                if p.suffix in (".sqlite3", ".db")
                            ],
                            key,
                        ),
                    }
                )
                if mode == "loader" and label == "credential_add":
                    for flag, value in (
                        ("account", False),
                        ("owner", False),
                        ("private", False),
                        ("group", True),
                    ):
                        hits.clear()
                        try:
                            parts = await send(
                                "A",
                                "ww获取token",
                                f"deny-{flag}",
                                "onebot",
                                **{flag: value},
                            )
                        except Exception as exc:
                            denied[flag] = {"error": type(exc).__name__}
                        else:
                            denied[flag] = {"parts": parts}
                        assert (
                            "<fixture-token>" not in str(denied[flag]) and not hits
                        ), (flag, denied[flag], hits)
    return {
        "cases": rows,
        "lifecycle": lifecycle,
        "sampled_background_httpx2": startup_http,
        "loader_negative_requests": denied,
        "account_handlers": [h for h in handlers if "user." in h or "resin." in h],
        "python": sys.version.split()[0],
    }


def compare(key, native_result, loader_result):
    """Require real state changes and matching content while reporting segment adaptation."""
    native_cases, loader_cases = native_result["cases"], loader_result["cases"]
    labels = [label for label, _, _ in scenarios(key)]
    assert native_result["reference"] == REFERENCE_COMMIT
    for result in (native_result, loader_result):
        assert (
            result["commit"] == LOCK[key]["commit"]
            and result["snapshot"] == LOCK[key]["snapshot_sha256"]
        )
    if key == "waves":
        assert set(loader_result["loader_negative_requests"]) == {
            "account",
            "owner",
            "private",
            "group",
        }
    for rows in (native_cases, loader_cases):
        assert [r["case"] for r in rows] == labels
    for result in (native_result, loader_result):
        assert (
            result["lifecycle"]["scheduler_started"]
            and result["lifecycle"]["scheduler_stopped"]
        )
    known_projection = {}
    http_differences = {}
    request_field_differences = {}
    for left, right in zip(native_cases, loader_cases, strict=True):
        label = left["case"]
        assert "error" not in left and "error" not in right, (label, left, right)
        if left["external_http"] != right["external_http"]:
            http_differences[label] = {
                "native": left["external_http"],
                "loader": right["external_http"],
            }

        # Stack locations distinguish implementations, not HTTP semantics. Compare
        # client-visible attempts (including pre-network rejection), full field
        # fingerprints, methods, response/exception, and ordered endpoints.
        def request_fields(case):
            return [
                {k: v for k, v in item.items() if k not in ("path", "rejected_at")}
                for item in case["http_details"]
            ]

        native_fields, loader_fields = request_fields(left), request_fields(right)
        if native_fields != loader_fields:
            request_field_differences[label] = {
                "native": native_fields,
                "loader": loader_fields,
            }
        assert left["state"] == right["state"], label
        bindings = []
        if label in ("bind", "duplicate", "other_query", "bound_query", "other_remove"):
            bindings = [UID_A]
        elif label == "second":
            bindings = [UID_B, UID_A] if key == "waves" else [UID_A, UID_B]
        elif label == "switch_second":
            bindings = [UID_B, UID_A]
        elif label == "switch":
            bindings = [UID_A, UID_B]
        elif label == "remove":
            bindings = [UID_B]
        elif label.startswith("credential_") and label != "credential_rejected":
            bindings = [UID_A]
        assert left["state"]["bindings"] == ({"A": bindings} if bindings else {}), label
        stored_credential = label.startswith("credential_") and label not in (
            "credential_rejected",
            "credential_remove",
            "credential_final",
        )
        assert left["state"]["credential_rows"] == (
            [["A", UID_A, True, True]] if stored_credential else []
        ), label
        parts = [p for p in left["parts"] if p.get("segment") != "log_INFO"]
        actual = right["parts"]
        if key == "waves" and label in ("bound_query", "switch_second", "switch"):
            assert len(actual) == 2 and actual[1]["text"].startswith("ww切换")
            actual = actual[:1]
        if label == "credential_get":
            parts = parts[0]["node"]
            assert any("<fixture-token>" in p.get("text", "") for p in parts)
        if label in (
            "credential_other",
            "credential_other_adapter",
            "credential_other_remove",
            "credential_final",
        ):
            assert not left["external_http"] and "<fixture-token>" not in str(parts), (
                label
            )
        assert parts and parts == actual, (label, parts, actual)
        if left["parts"] != right["parts"]:
            known_projection[label] = [
                [
                    next(iter(p.values())) if "segment" in p else next(iter(p))
                    for p in row["parts"]
                ]
                for row in (left, right)
            ]
    return {
        "profile": key,
        "cases": len(labels),
        "verified": [
            "committed_rows",
            "actor_isolation",
            "response_content",
        ]
        + ([] if http_differences or request_field_differences else ["http_requests"]),
        "http_differences": http_differences,
        "request_field_differences": request_field_differences,
        "matched_http_cases": len(labels)
        - len(http_differences.keys() | request_field_differences.keys()),
        "status": "http_differences"
        if http_differences or request_field_differences
        else "matched",
        "lifecycle": {
            name: r["lifecycle"]
            for name, r in (("native", native_result), ("loader", loader_result))
        },
        "segment_differences": known_projection,
        "commit": native_result["commit"],
        "worker_sha256": loader_result["worker_sha256"],
    }


def compare_waves_policies(native, frozen, offline):
    comparisons = [compare("waves", native, other) for other in (frozen, offline)]
    assert (
        frozen["worker_origin"] == "frozen" and frozen["lifecycle"]["resource_broker"]
    )
    assert (
        offline["worker_origin"] == "rebuilt_offline"
        and not offline["lifecycle"]["resource_broker"]
    )
    assert [r["matched_http_cases"] for r in comparisons] == [19, 22]
    assert set(comparisons[0]["request_field_differences"]) == {
        "credential_rejected",
        "credential_add",
        "credential_get",
    }
    assert set(comparisons[0]["http_differences"]) == {"credential_rejected"}
    native_rows = {r["case"]: r for r in native["cases"]}
    frozen_rows = {r["case"]: r for r in frozen["cases"]}

    def probes(rows):
        return [
            h
            for h in rows["credential_rejected"]["http_details"]
            if h["client"] == "httpx"
        ]

    native_probes, frozen_probes = probes(native_rows), probes(frozen_rows)
    urls = [
        "https://event.kurobbs.com/event/ip",
        "https://api.ipify.org/?format=json",
        "https://httpbin.org/ip",
    ]
    assert [p["endpoint"] for p in native_probes] == [["GET", urls[0]]]
    assert native_probes[0]["status"] == 200
    assert [p["endpoint"] for p in frozen_probes] == [["GET", url] for url in urls]
    for row in native_probes + frozen_probes:
        assert (
            row["payload_bytes"] == 0
            and not {"cookie", "authorization", "token"} & row["headers"].keys()
        )
    for row in frozen_probes:
        assert row["error"] == "Unsupported" and row["rejected_at"][-1][:2] == [
            "resource_httpx.py",
            "handle_async_request",
        ]
    changed = []
    for label, left in native_rows.items():

        def posts(row):
            return [h for h in row["http_details"] if h["client"] == "aiohttp"]

        for a, b in zip(posts(left), posts(frozen_rows[label]), strict=True):
            assert a["endpoint"] == b["endpoint"] and a["form"] == b["form"]
            if a["headers"] != b["headers"]:
                assert a["headers"].keys() == b["headers"].keys()
                assert {
                    k for k in a["headers"] if a["headers"][k] != b["headers"][k]
                } == {"devcode"}
                assert a["headers"]["devcode"].startswith("<fixture-ip>:") and b[
                    "headers"
                ]["devcode"].startswith("<upstream-default-ip>:")
                changed.append([label, a["endpoint"][1]])
    assert changed == [
        ["credential_add", "/aki/roleBox/requestToken"],
        ["credential_add", "/aki/roleBox/akiBox/refreshData"],
        ["credential_add", "/aki/roleBox/akiBox/baseData"],
        ["credential_get", "/aki/roleBox/akiBox/refreshData"],
    ]
    return {
        "frozen_worker": frozen["worker_sha256"],
        "offline_worker": offline["worker_sha256"],
        "business_cases_matched": 22,
        "frozen_full_http_matches": 19,
        "offline_full_http_matches": 22,
        "changed_header": "devCode",
        "affected_posts": changed,
        "frozen_rejected_probes": frozen_probes,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=LOCK, required=True)
    parser.add_argument(
        "--mode", choices=["native", "loader", "compare", "policy"], required=True
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--child", action="store_true")
    parser.add_argument(
        "--mys-session",
        action="store_true",
        help="exercise the native built-in Cookie issuer and ordinary game session query",
    )
    parser.add_argument(
        "--mys-stored",
        action="store_true",
        help="seed the owner's synthetic GsUser row through the public model in a normal account request",
    )
    parser.add_argument("--libraries", type=Path)
    parser.add_argument("--native-result", type=Path)
    parser.add_argument("--loader-result", type=Path)
    parser.add_argument("--offline-result", type=Path)
    parser.add_argument(
        "--worker", type=Path, help="use these frozen pyz bytes without rebuilding"
    )
    parser.add_argument("--worker-sha256", help="expected hex digest of --worker")
    args = parser.parse_args()
    if args.mys_stored:
        args.mys_session = True
    if args.mys_session and (args.profile == "waves" or args.mode == "policy"):
        parser.error(
            "--mys-session applies to genshin/starrail native, loader or compare runs"
        )
    exit_code = 0
    if bool(args.worker) != bool(args.worker_sha256) or (
        args.worker and args.mode != "loader"
    ):
        parser.error(
            "--worker and --worker-sha256 must be supplied together in loader mode"
        )
    if args.mode == "policy":
        if args.profile != "waves" or not all(
            (args.native_result, args.loader_result, args.offline_result)
        ):
            parser.error("policy comparison requires Waves and all three receipts")
        result = compare_waves_policies(
            *(
                json.loads(p.read_bytes())
                for p in (args.native_result, args.loader_result, args.offline_result)
            )
        )
    elif args.mode == "compare" and args.mys_session:
        result = mys_session.verify(
            args.profile,
            json.loads(args.native_result.read_bytes()),
            json.loads(args.loader_result.read_bytes()),
            args.mys_stored,
            LOCK[args.profile],
            REFERENCE_COMMIT,
        )
    elif args.mode == "compare":
        result = compare(
            args.profile,
            json.loads(args.native_result.read_bytes()),
            json.loads(args.loader_result.read_bytes()),
        )
        exit_code = int(result["status"] != "matched")
    elif args.child:
        lifecycle = {"phase": "initialize", "finished": [], "logged_errors": []}
        with ExitStack() as outer:
            try:
                result = asyncio.run(
                    child(
                        args.profile,
                        args.mode,
                        Path.cwd(),
                        args.libraries,
                        outer,
                        lifecycle,
                        args.mys_session,
                        args.mys_stored,
                    )
                )
            except Exception as exc:
                result = {
                    "status": "child_failed",
                    "error": type(exc).__name__,
                    "lifecycle": lifecycle,
                }
                traceback.print_exc()
                exit_code = 1
    else:
        spec = LOCK[args.profile]
        exe, meta = selected(
            LOCAL / args.profile,
            (ROOT / "profiles" / f"{args.profile}-requirements.txt").read_bytes(),
        )
        libraries = (
            exe.parent.parent / "Lib/site-packages"
            if os.name == "nt"
            else next((exe.parent.parent / "lib").glob("python*/site-packages"))
        )
        with tempfile.TemporaryDirectory(dir=LOCAL, prefix="upstream-account-") as tmp:
            work = Path(tmp)
            source = work / "plugins"
            evidence = {}
            if args.mode == "native":
                evidence = copy_reference(LOCAL / "reference", work)
                source = work / "gsuid_core/plugins"
            else:
                blob = (
                    args.worker.read_bytes()
                    if args.worker
                    else worker_archive(spec, offline=True)
                )
                actual = hashlib.sha256(blob).hexdigest()
                if args.worker and actual != args.worker_sha256.lower():
                    raise ValueError("supplied worker SHA256 mismatch")
                (work / "worker.pyz").write_bytes(blob)
                evidence.update(
                    worker_sha256=actual,
                    worker_origin="frozen" if args.worker else "rebuilt_offline",
                )
            archive = (
                LOCAL / "runtime" / f"{spec['package']}-{spec['snapshot_sha256']}.zip"
            )
            with archive.open("rb") as stream:
                assert (
                    hashlib.file_digest(stream, "sha256").hexdigest()
                    == spec["snapshot_sha256"]
                )
                stream.seek(0)
                with ZipFile(stream) as z:
                    z.extractall(source)
            home = work / "private-home"
            home.mkdir()
            env = {
                k: v
                for k, v in os.environ.items()
                if k.upper() in ("SYSTEMROOT", "WINDIR", "LANG", "PATH")
            }
            env.update(
                {
                    k: str(home)
                    for k in (
                        "HOME",
                        "USERPROFILE",
                        "APPDATA",
                        "LOCALAPPDATA",
                        "TEMP",
                        "TMP",
                        "XDG_CACHE_HOME",
                    )
                }
            )
            out = work / "observed.json"
            command = [
                sys.executable if args.mode == "native" else str(exe),
                "-IB",
                "-X",
                "utf8",
                str(Path(__file__).resolve()),
                "--child",
                *(["--mys-session"] if args.mys_session else []),
                *(["--mys-stored"] if args.mys_stored else []),
                "--profile",
                args.profile,
                "--mode",
                args.mode,
                "--libraries",
                str(libraries),
                "--output",
                str(out),
            ]
            try:
                # Original upstream debug messages may contain request credentials.
                quiet = subprocess.DEVNULL if args.mys_session else None
                exit_code = subprocess.run(
                    command, cwd=work, env=env, timeout=240, stdout=quiet, stderr=quiet
                ).returncode
            except subprocess.TimeoutExpired:
                exit_code = 124
            result = {
                **(
                    json.loads(out.read_bytes())
                    if out.is_file()
                    else {"status": "child_did_not_finish", "returncode": exit_code}
                ),
                **evidence,
                "reference": REFERENCE_COMMIT,
                "commit": spec["commit"],
                "snapshot": spec["snapshot_sha256"],
            }
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8"
    )
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
