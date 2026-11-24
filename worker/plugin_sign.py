"""Scope framework sign-result handling to an explicit, live owner transport."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json

import plugin_bindings
from plugin_mys import BaseMysApi, _account
from port_errors import Unsupported


@dataclass
class _SignGrant:
    owner: object
    account: object
    uid: str
    game: str
    transport: object
    mark_invalid: object
    active: bool = True


_current = ContextVar("sayu_sign_grant", default=None)


@contextmanager
def sign_scope(uid, game, transport, *, mark_invalid=None):
    from httpx2 import MockTransport

    if not isinstance(transport, MockTransport):
        raise Unsupported("only an in-memory sign transport is available")
    owner = plugin_bindings._current.get()
    account = _account(uid, game)
    if not owner or not owner.active or not owner.writable or account is None:
        raise Unsupported(
            "a private owner session is required to issue a sign transport"
        )
    grant = _SignGrant(owner, account, uid, game, transport, mark_invalid)
    token = _current.set(grant)
    try:
        yield
    finally:
        grant.active = False
        _current.reset(token)


def _authority(uid, game):
    grant = _current.get()
    if (
        grant is None
        or not grant.active
        or grant.uid != uid
        or grant.game != game
        or grant.owner is not plugin_bindings._current.get()
        or grant.account is not _account(uid, game)
    ):
        raise Unsupported(
            "account-changing sign-in requires a dedicated transport grant"
        )
    return grant


class SignWireBase(BaseMysApi):
    async def _mys_request(
        self,
        url,
        method="GET",
        header=None,
        params=None,
        data=None,
        *,
        base_url,
        game_name,
    ):
        from httpx2 import AsyncClient

        issued = _current.get()
        if issued is None:
            raise Unsupported("sign transport unavailable")
        grant = _authority(issued.uid, game_name)
        if method not in {"GET", "POST"}:
            raise Unsupported("unapproved sign request method")
        headers = dict(header or {})
        account = grant.account
        if headers.get("Cookie") != account.cookie:
            raise Unsupported("sign request owner mismatch")
        uid = (params or data or {}).get("uid")
        if uid is not None:
            if uid != grant.uid:
                raise Unsupported("sign request owner mismatch")
            if account.device_id:
                headers.setdefault("x-rpc-device_id", account.device_id)
            if account.fp:
                headers.setdefault("x-rpc-device_fp", account.fp)
        target = base_url + url
        if "DS" in headers:
            if account.authorize is None:
                raise Unsupported("sign request issuer is unavailable")
            supplied = account.authorize(
                method, target, dict(params or {}), data, headers.pop("DS"), "", data
            )
            header_value = next(
                (v for k, v in (supplied or {}).items() if k.lower() == "ds"), None
            )
            if not header_value:
                return -51
            headers["DS"] = header_value
        async with AsyncClient(
            transport=grant.transport,
            follow_redirects=False,
            trust_env=False,
            timeout=10,
        ) as client:
            async with client.stream(
                method, target, params=params, json=data, headers=headers
            ) as response:
                buf = bytearray()
                async for part in response.aiter_bytes(chunk_size=65536):
                    if len(buf) + len(part) > 512 * 1024:
                        raise Unsupported("oversized sign response")
                    buf.extend(part)
        result = json.loads(buf)
        code = result.get("retcode", result.get("code", 0))
        return code if code else result


async def sign_error(uid, retcode, game_name="gs"):
    from gsuid_core.utils.error_reply import get_error

    # The worker cannot invalidate shared credentials; this side effect, where
    # supported, is owned by the caller's ephemeral sign operation.
    if retcode in (10001, -100) and _account(uid, game_name) is not None:
        invalidate = _authority(uid, game_name).mark_invalid
        if invalidate is None:
            raise Unsupported("private sign status callback unavailable")
        await invalidate(uid, game_name)
    return f"[{game_name}] 签到失败!{get_error(retcode)}"


async def sign_in(uid: str, game_name: str = "gs") -> str:
    from gsuid_core.utils.api.mys_api import mys_api

    # Match native validation before resolving any private account.
    mys_api.check_os(uid, game_name)
    if game_name not in {"gs", "sr", "zzz"}:
        raise KeyError(game_name)
    if await mys_api.get_ck(uid, "OWNER", game_name) is None:
        return await sign_error(uid, -51, game_name)
    _authority(uid, game_name)
    from plugin_sign_result import sign_in as format_result

    return await format_result(uid, game_name)


def install_sign(package):
    package("gsuid_core.utils.sign")
    package("gsuid_core.utils.sign.sign").sign_in = sign_in
