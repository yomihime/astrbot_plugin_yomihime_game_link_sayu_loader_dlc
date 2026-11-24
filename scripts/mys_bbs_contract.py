"""Compare anonymous BBS reads at the original HTTP boundary."""

import argparse
import asyncio
import hashlib
import json
import logging
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

from mys_account_contract import Capture, COOKIE
from native_contract import ROOT, REFERENCE_COMMIT, copy_reference, offline


async def exercise(api, capture):
    results = {}
    for name, method, args, payload in (
        (
            "public_post",
            "get_bbs_post_detail",
            ("10001",),
            {"retcode": 0, "data": {"post": {"subject": "Fixture"}}},
        ),
        ("missing_post", "get_bbs_post_detail", ("10002",), {"code": -42}),
        ("missing_data", "get_bbs_post_detail", ("10003",), {"retcode": 0}),
        (
            "collection",
            "get_bbs_collection",
            ("50001",),
            {"retcode": 0, "data": {"posts": ["p"]}},
        ),
        (
            "empty_collection",
            "get_bbs_collection",
            ("50002", "6"),
            {"retcode": 0, "data": {}},
        ),
    ):
        capture.payload = payload
        try:
            value = {"value": await getattr(api, method)(*args)}
        except Exception as exc:
            value = {"exception": type(exc).__name__, "detail": str(exc)}
        results[name] = value
    return results


async def run():
    sys.addaudithook(offline)
    from gsuid_core.utils.api.mys_api import _MysApi
    import httpx2
    from plugin_bindings import binding_scope
    from plugin_mys import Account, MysApi, account_scope

    native, product = Capture(), Capture()

    with native.native_transport():
        reference = await exercise(_MysApi(), native)
    api = MysApi()
    client = httpx2.AsyncClient
    with patch.object(
        httpx2,
        "AsyncClient",
        lambda **kw: client(transport=httpx2.MockTransport(product.worker_http), **kw),
    ):
        try:
            await api.get_bbs_post_detail("10001")
        except NotImplementedError as exc:
            unavailable = str(exc)
        else:
            raise AssertionError("missing issuer must stop before HTTP")
        assert not product.calls
        api._authorize_public_bbs = (
            lambda method, url, params: "fixture-public-signature"
        )
        actual = await exercise(api, product)
        assert (
            reference == actual
            and native.calls == product.calls
            and len(product.calls) == 5
        )
        with (
            binding_scope("actor", "adapter", True, writable=True),
            account_scope("gs", Account("10001", COOKIE)),
        ):
            product.payload = {"retcode": 0, "data": {"post": "same public read"}}
            assert (await api.get_bbs_post_detail("10001"))["post"]
    assert all(
        "cookie" not in h and "x-rpc-device_id" not in h and "x-rpc-device_fp" not in h
        for h in native.prepared + product.prepared
    )
    assert actual["public_post"] == {"value": {"post": {"subject": "Fixture"}}}
    assert (
        actual["missing_post"] == {"value": -42}
        and actual["missing_data"]["exception"] == "KeyError"
    )
    assert actual["collection"] == {"value": ["p"]} and actual["empty_collection"] == {
        "value": -500
    }
    return {
        "reference": REFERENCE_COMMIT,
        "worker_sha256": hashlib.sha256(
            (ROOT / "worker/plugin_mys.py").read_bytes()
        ).hexdigest(),
        "cases": actual,
        "requests": native.calls,
        "headers": {"native": native.prepared, "loader": product.prepared},
        "without_issuer": {"exception": unavailable, "http_calls": 0},
        "scope": "Native generated the web signature. Loader used an explicitly injected test issuer and omitted the original mobile client headers. Neither sent owner credentials or device identifiers. No live signature, profile session, or endpoint acceptance was verified.",
    }


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf8")
    sys.stderr.reconfigure(encoding="utf8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="sayu-bbs-") as tmp:
        work = Path(tmp)
        copy_reference(ROOT / ".sayu-local/reference", work)
        sys.path[:0] = [str(work), str(ROOT / "worker")]
        try:
            report = asyncio.run(run())
        finally:
            logging.shutdown()
            sys.excepthook = sys.__excepthook__
    args.output.write_text(json.dumps(report, indent=2), encoding="utf8")
    print("Matched five original public BBS outcomes with an injected web issuer.")
