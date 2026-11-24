"""Compare sign_in resolution and empty-account behavior without a game service."""

import argparse
import asyncio
from contextlib import ExitStack
import inspect
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build_local import worker_archive  # noqa: E402
from native_contract import copy_reference, offline, REFERENCE_COMMIT  # noqa: E402


class FixtureTransport:
    def __init__(self, case):
        self.case = case
        self.calls = []

    def respond(self, method, url, params, body, headers):
        normalized = {k.lower(): v for k, v in headers.items()}
        # Compare application headers, excluding implicit HTTP client defaults.
        observed = {
            k: v
            for k, v in normalized.items()
            if k.startswith("x")
            or k in ("referer", "origin", "cookie", "ds")
            or (k == "user-agent" and "x-rpc-app_version" in normalized)
        }
        for k in ("cookie", "ds", "x-rpc-device_id", "x-rpc-device_fp"):
            if k in observed:
                assert (
                    observed[k]
                    == {
                        "cookie": "test",
                        "ds": "fixture-approval",
                        "x-rpc-device_id": "device",
                        "x-rpc-device_fp": "fp",
                    }[k]
                ), k
                observed[k] = "<fixture>"
        self.calls.append(
            {
                "method": method,
                "url": url,
                "query": dict(params),
                "json": body,
                "headers": observed,
            }
        )
        if url.endswith("/info"):
            value = (
                -52
                if self.case == "lookup_error"
                else {
                    "is_sign": self.case == "already" or len(self.calls) > 1,
                    "today": "2026-01-05",
                    "total_sign_day": 0 if len(self.calls) == 1 else 1,
                }
            )
        elif url.endswith("/sign"):
            value = {
                "expired": -100,
                "challenge": {"risk_code": 375},
                "overseas": {"code": "ok"},
            }.get(self.case, {"risk_code": 0})
        elif url.endswith("/home"):
            value = {"awards": [{"name": "fixture", "cnt": 4}]}
        else:
            raise AssertionError("unexpected sign endpoint")
        return (
            {"retcode": value}
            if isinstance(value, int)
            else {"retcode": 0, "data": value}
        )

    def worker(self, req):
        from httpx2 import Response

        payload = self.respond(
            req.method,
            str(req.url.copy_with(query=None)),
            req.url.params,
            json.loads(req.content) if req.content else None,
            req.headers,
        )
        return Response(200, json=payload)

    def native(self, session, method, url, **kwargs):
        from aiohttp import ClientRequest
        from yarl import URL

        req = ClientRequest(
            method,
            URL(url),
            params=kwargs["params"],
            headers=session._prepare_headers(kwargs["headers"]),
            session=session,
        )
        payload = self.respond(
            method,
            str(req.url.with_query(None)),
            req.url.query,
            kwargs["json"],
            req.headers,
        )

        class Reply:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            async def json(self):
                return payload

        return Reply()

    async def mark_invalid(self, uid, game):
        self.calls.append(["mark_invalid", uid, game])


async def exercise_grants(mode, sign_in):
    result = {}
    for case in (
        "lookup_error",
        "already",
        "expired",
        "challenge",
        "domestic",
        "overseas",
    ):
        port = FixtureTransport(case)
        uid = "700000001" if case == "overseas" else "100000001"
        game = "sr"
        with ExitStack() as stack:
            if mode == "native":
                from aiohttp import ClientSession
                from gsuid_core.utils.api.mys import sign_request
                from gsuid_core.utils.sign.sign import GsUser

                async def owned(uid, game_name):
                    return "test"

                async def attributes(uid, field, game_name):
                    return {"fp": "fp", "device_id": "device"}.get(field)

                async def invalidate(uid, game_name, **updates):
                    assert updates == {"status": "error"}
                    await port.mark_invalid(uid, game_name)

                stack.enter_context(
                    patch.object(
                        ClientSession,
                        "request",
                        lambda session, method, url, **kw: port.native(
                            session, method, url, **kw
                        ),
                    )
                )
                stack.enter_context(
                    patch.object(GsUser, "get_user_attr_by_uid", attributes)
                )
                stack.enter_context(
                    patch.object(
                        sign_request, "generate_os_ds", lambda: "fixture-approval"
                    )
                )
                stack.enter_context(
                    patch.object(
                        sign_request, "get_web_ds_token", lambda _: "fixture-approval"
                    )
                )
                stack.enter_context(
                    patch.object(GsUser, "get_user_cookie_by_uid", owned)
                )
                stack.enter_context(
                    patch.object(
                        GsUser, "update_data_by_uid_without_bot_id", invalidate
                    )
                )
            else:
                from plugin_bindings import binding_scope
                from plugin_mys import Account, account_scope
                from plugin_sign import sign_scope

                stack.enter_context(
                    binding_scope("fixture", "web", True, writable=True)
                )
                from httpx2 import MockTransport

                account = Account(
                    uid,
                    "test",
                    authorize=lambda *args: {"DS": "fixture-approval"},
                    device_id="device",
                    fp="fp",
                )
                stack.enter_context(account_scope(game, account))
                stack.enter_context(
                    sign_scope(
                        uid,
                        game,
                        MockTransport(port.worker),
                        mark_invalid=port.mark_invalid,
                    )
                )
            text = await sign_in(uid, game)
        result[case] = {"result": text, "calls": port.calls}
    return result


async def observe(mode, archive):
    work = Path.cwd()
    sys.addaudithook(offline)
    if mode == "native":
        sys.path.insert(0, str(work))
        from gsuid_core.utils.database import base_models as db

        await db.init_database()
        from gsuid_core.utils.database.startup import ensure_core_database_tables

        await ensure_core_database_tables()
    else:
        sys.path.insert(0, str(archive))
        import sayu_compat
        from plugin_runtime import PluginRuntime

        sayu_compat.install("Fixture", "fx", work, source_root=work)
        runtime = PluginRuntime(work)
        runtime.install(work, "Fixture")

    from gsuid_core.utils.sign.sign import sign_in

    results = {"parameters": list(inspect.signature(sign_in).parameters)}
    for uid, game in (
        ("100000001", "gs"),
        ("700000001", "sr"),
        ("1000000001", "zzz"),
        ("bad", "sr"),
        ("", "gs"),
        ("100000001", "other"),
    ):
        try:
            value = await sign_in(uid, game)
        except Exception as exc:
            value = type(exc).__name__
        results[uid + "-" + game] = value
    results["scoped_operations"] = await exercise_grants(mode, sign_in)
    if mode == "loader":
        from plugin_bindings import binding_scope
        from plugin_mys import Account, account_scope
        from port_errors import Unsupported

        with (
            binding_scope("fixture", "web", True, writable=True),
            account_scope("sr", Account("700000001", "test")),
        ):
            try:
                await sign_in("700000001", "sr")
            except Unsupported:
                pass
            else:
                raise AssertionError("account write was not denied")
    else:
        await db.engine.dispose()
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("native", "loader"))
    parser.add_argument("--archive", type=Path)
    parser.add_argument(
        "--reference", type=Path, default=ROOT / ".sayu-local/reference"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.mode:
        report = asyncio.run(observe(args.mode, args.archive))
    else:
        report = {"reference": REFERENCE_COMMIT}
        with tempfile.TemporaryDirectory(prefix="sayu-sign-") as temp:
            archive = Path(temp) / "worker.pyz"
            archive.write_bytes(worker_archive({"package": "Fixture", "prefix": "fx"}))
            for mode in ("native", "loader"):
                work = Path(temp) / mode
                work.mkdir()
                if mode == "native":
                    copy_reference(args.reference, work)
                output = work / "result.json"
                subprocess.run(
                    [
                        sys.executable,
                        "-IB",
                        "-X",
                        "utf8",
                        __file__,
                        "--mode",
                        mode,
                        "--archive",
                        str(archive),
                        "--output",
                        str(output),
                    ],
                    cwd=work,
                    timeout=60,
                    check=True,
                )
                report[mode] = json.loads(output.read_bytes())
        if report["native"] != report["loader"]:
            raise AssertionError(report)
        assert "签到失败!" in report["loader"]["700000001-sr"]
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
    )


if __name__ == "__main__":
    main()
