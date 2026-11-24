"""Compare private record queries against a temporary native database and fake HTTP."""

import argparse
import asyncio
from dataclasses import replace
import hashlib
from http.client import parse_headers
from io import BytesIO
import json
import logging
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

from native_contract import ROOT, REFERENCE_COMMIT, copy_reference, offline


UID = "100000001"
OTHER = "100000002"
COOKIE = "fixture-session"
DEVICE = "fixture-device"
FP = "fixture-fp"
SR_UID = "700000001"


class Capture:
    def __init__(self):
        self.calls = []
        self.prepared = []
        self.payload = {}

    def record(self, method, url, headers, params):
        headers = {k.lower(): v for k, v in headers.items()}
        redacted = dict(headers)
        for key, marker in (
            ("cookie", "owner test cookie"),
            ("ds", "signature present"),
            ("x-rpc-device_id", "fixture device ID"),
            ("x-rpc-device_fp", "fixture device FP"),
        ):
            if key in redacted:
                redacted[key] = f"<{marker}>"
        self.prepared.append(redacted)
        self.calls.append(
            {
                "method": method,
                "url": str(url),
                "query": dict(params),
                "owner_cookie": headers.get("cookie") == COOKIE,
                "device_id": headers.get("x-rpc-device_id") == DEVICE,
                "device_fp": headers.get("x-rpc-device_fp") == FP,
                "signed": bool(headers.get("ds")),
            }
        )

    def native_transport(self):
        from aiohttp import TCPConnector
        from yarl import URL

        capture = self

        async def connect(connector, request, traces, timeout):
            loop = asyncio.get_running_loop()
            protocol = connector._factory()

            class Wire(asyncio.Transport):
                closed = False

                def is_closing(self):
                    return self.closed

                def close(self):
                    if not self.closed:
                        self.closed = True
                        loop.call_soon(protocol.connection_lost, None)

                abort = close

                def write(self, data):
                    buffer.extend(data)
                    if b"\r\n\r\n" not in buffer:
                        return
                    head, _, body = buffer.partition(b"\r\n\r\n")
                    if body:
                        raise AssertionError("unexpected body in record GET")
                    lines = BytesIO(head + b"\r\n\r\n")
                    method, target, _ = lines.readline().decode("ascii").split()
                    url = request.url.origin().join(URL(target, encoded=True))
                    capture.record(
                        method, url.with_query(None), parse_headers(lines), url.query
                    )
                    payload = json.dumps(capture.payload).encode()
                    reply = (
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
                        + str(len(payload)).encode()
                        + b"\r\n\r\n"
                        + payload
                    )
                    loop.call_soon(protocol.data_received, reply)
                    buffer.clear()

            buffer = bytearray()
            protocol.connection_made(Wire())
            return protocol

        return patch.object(TCPConnector, "_create_connection", connect)

    def worker_http(self, req):
        from httpx2 import Response

        self.record(
            req.method, req.url.copy_with(query=None), req.headers, req.url.params
        )
        return Response(200, json=self.payload)


async def exercise(api, endpoint, capture):
    result = []
    actions = [
        ("missing_owner", lambda: api.endpoint_request(endpoint, OTHER)),
        ("daily_data", lambda: api.get_daily_data(UID)),
        ("index", lambda: api.get_info(UID)),
        ("index_cached", lambda: api.get_info(UID)),
        ("index_explicit_cookie", lambda: api.get_info(UID, ck=COOKIE)),
        ("gcg", lambda: api.get_gcg_info(UID)),
        ("deck", lambda: api.get_gcg_deck(UID)),
        ("abyss_previous", lambda: api.get_spiral_abyss_info(UID, "2")),
        ("abyss_cached", lambda: api.get_spiral_abyss_info(UID, "2")),
        ("abyss_error_code", lambda: api.get_spiral_abyss_info(UID, "1")),
        ("abyss_cached_error", lambda: api.get_spiral_abyss_info(UID, "1")),
        ("error_code", lambda: api.get_daily_data(UID)),
        ("missing_data", lambda: api.get_daily_data(UID)),
        (
            "by_endpoint",
            lambda: api.endpoint_request(
                endpoint, UID, params={"role_id": UID, "sample": True}, ds_mode="none"
            ),
        ),
        (
            "by_region",
            lambda: api.simple_mys_req_by_region(
                endpoint, False, params={"role_id": UID}, cookie=COOKIE
            ),
        ),
        (
            "by_uid",
            lambda: api._mys_req_get(endpoint, False, {"uid": UID}),
        ),
    ]
    for label, action in actions:
        start = len(capture.calls)
        capture.payload = {"retcode": 0, "data": {"example": label}}
        if label in ("error_code", "abyss_error_code"):
            capture.payload = {"code": -42}
        elif label == "missing_data":
            capture.payload = {"retcode": 0}
        try:
            outcome = {"value": await action()}
        except Exception as exc:
            outcome = {"error": type(exc).__name__, "message": str(exc)}
        result.append({"case": label, **outcome, "requests": capture.calls[start:]})
    return result


async def overseas_record(api, endpoint_cls, capture):
    endpoint = endpoint_cls(
        cn="https://api-takumi-record.mihoyo.com/game_record/app/hkrpg/api/note",
        os="https://bbs-api-os.hoyolab.com/game_record/hkrpg/api/note",
        name="STAR_RAIL_NOTE",
    )
    capture.payload = {"retcode": 0, "data": {"current_stamina": 160}}
    start = len(capture.calls)
    value = await api.simple_mys_req(endpoint, SR_UID, game_name="sr")
    return {"case": "starrail_os", "value": value, "requests": capture.calls[start:]}


async def main_async(work, worker):
    sys.addaudithook(offline)
    from gsuid_core.utils.database import base_models as db

    await db.init_database()
    from gsuid_core.utils.database.startup import ensure_core_database_tables
    from gsuid_core.utils.database.models import GsUser
    from gsuid_core.utils.api.mys_api import _MysApi
    from gsuid_core.utils.api.mys import api as native_endpoints
    from gsuid_core.utils.api.mys.api import ApiEndpoint, DAILY_NOTE

    await ensure_core_database_tables()
    async with db.async_maker() as session:
        session.add(
            GsUser(
                user_id="actor",
                bot_id="adapter",
                uid=UID,
                cookie=COOKIE,
                fp=FP,
                device_id=DEVICE,
                region="cn_gf01",
                sr_uid=SR_UID,
                sr_region="prod_official_euro",
            )
        )
        await session.commit()
    native_capture = Capture()

    with native_capture.native_transport():
        native_api = _MysApi()
        native = await exercise(native_api, DAILY_NOTE, native_capture)
        native.append(await overseas_record(native_api, ApiEndpoint, native_capture))
    await db.engine.dispose()

    sys.path.insert(0, str(worker))
    import plugin_mys as mys
    import httpx2
    from plugin_bindings import binding_scope

    for name, product in mys.PUBLIC_ENDPOINT_DECLARATIONS.items():
        reference = getattr(native_endpoints, name)
        if (reference.cn, reference.os, reference.name) != (
            product.cn,
            product.os,
            product.name,
        ):
            raise ValueError(f"endpoint mismatch for {name}")

    session = mys.Account(
        UID,
        COOKIE,
        lambda *args: {"DS": "test-authorization"},
        device_id=DEVICE,
        fp=FP,
    )
    loader_capture = Capture()
    client = httpx2.AsyncClient
    with (
        binding_scope("actor", "adapter", True, writable=True),
        mys.account_scope("gs", session),
        patch.object(
            httpx2,
            "AsyncClient",
            lambda **kw: client(
                transport=httpx2.MockTransport(loader_capture.worker_http), **kw
            ),
        ),
    ):
        api = mys.MysApi()
        result = await exercise(api, mys.ENDPOINTS["DAILY_NOTE"], loader_capture)
        with mys.account_scope("sr", replace(session, uid=SR_UID)):
            result.append(await overseas_record(api, mys.ApiEndpoint, loader_capture))
        for original, actual in zip(native, result, strict=True):
            if original != actual:
                raise ValueError((original, actual))
        expected = [
            -51,
            {"example": "daily_data"},
            {"example": "index"},
            {"example": "index"},
            {"example": "index_explicit_cookie"},
            {"example": "gcg"},
            {"example": "deck"},
            {"example": "abyss_previous"},
            {"example": "abyss_previous"},
            -42,
            -42,
            -42,
            "KeyError",
            {"retcode": 0, "data": {"example": "by_endpoint"}},
            {"retcode": 0, "data": {"example": "by_region"}},
            {"retcode": 0, "data": {"example": "by_uid"}},
            {"retcode": 0, "data": {"current_stamina": 160}},
        ]
        if [r.get("value", r.get("error")) for r in result] != expected:
            raise ValueError(result)
        trace = len(loader_capture.calls)
        with binding_scope("other", "adapter", True):
            if await api.get_info(UID) != -51:
                raise AssertionError("credential crossed owner boundary")
        if len(loader_capture.calls) != trace:
            raise AssertionError("denied request reached HTTP")
        headers = []
        for case in native:
            for _ in case["requests"]:
                index = len(headers)
                original = native_capture.prepared[index]
                actual = loader_capture.prepared[index]
                headers.append(
                    {
                        "case": case["case"],
                        "native": original,
                        "loader": actual,
                        "unequal_header_names": sorted(
                            key
                            for key in original.keys() | actual.keys()
                            if original.get(key) != actual.get(key)
                        ),
                        "signature": "native generated; loader synthetic issuer, digest unverified"
                        if "ds" in original
                        else "unsigned; no issuer called",
                    }
                )
        loader_capture.payload = {"retcode": 1034}
        challenge = await api.get_daily_data(UID)
        if challenge != 1034 or len(loader_capture.calls) != trace + 1:
            raise AssertionError(
                "an account challenge must terminate after the first HTTP reply"
            )
    return result, headers


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reference", type=Path, default=ROOT / ".sayu-local/reference"
    )
    parser.add_argument("--worker", type=Path, default=ROOT / "worker")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="mys-reference-") as tmp:
        work = Path(tmp)
        copy_reference(args.reference, work)
        sys.path.insert(0, str(work))
        try:
            result, headers = asyncio.run(main_async(work, args.worker.resolve()))
        finally:
            # The native core installs a Windows file logger in the temporary tree.
            logging.shutdown()
            sys.excepthook = sys.__excepthook__
    if args.output:
        args.output.write_text(
            json.dumps(
                {
                    "reference": REFERENCE_COMMIT,
                    "worker_sha256": hashlib.sha256(
                        (args.worker / "plugin_mys.py").read_bytes()
                    ).hexdigest(),
                    "cases": result,
                    "headers": headers,
                    "scope": {
                        "native_boundary": "aiohttp built and serialized the request. An in-memory asyncio.Transport recorded its actual wire request line and headers and supplied an HTTP/1.1 JSON reply to the original aiohttp response parser.",
                        "loader_boundary": "httpx2 MockTransport received a prepared request; no live endpoint contacted",
                        "identity": "native read the synthetic cookie, device ID and device FP from a temporary database; loader read identical test values from an active owner lease",
                        "signature": "native generated its digest from the fixture; the signed loader cases used an explicitly synthetic issuer. No digest equality or online authorization was tested",
                        "unsigned": "ds_mode=none and _mys_req_get use the normal loader path without an issuer. Standard transport and upstream app headers may differ (listed per request)",
                        "returns": "matched the reported responses, missing-data exception, ordinary numeric errors and number of requests for the simulated inputs",
                        "differences": "cross-owner credential fallback is denied, a challenge stops on its first reply; native retries some transport errors and may send a bot message, whereas the loader propagates transport/decoding exceptions",
                    },
                    "loader_only": {
                        "second_actor": {"value": -51, "http_calls": 0},
                        "challenge": {"value": 1034, "http_calls": 1},
                    },
                },
                indent=2,
            ),
            "utf8",
        )
    print(
        f"Matched {len(result)} native account query cases and denied a second actor."
    )


if __name__ == "__main__":
    main()
