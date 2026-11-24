"""Owner-scoped, read-only game record queries for Sayu package plugins."""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from importlib import import_module
import json
from pathlib import Path
from random import choice
import sys
import time
from typing import Callable

import plugin_bindings


RECORD_BASE = "https://api-takumi-record.mihoyo.com"
BBS_URL = "https://bbs-api.mihoyo.com"
RECORD_BASE_OS = "https://bbs-api-os.hoyolab.com"
SERVER = dict(
    zip(
        "123456789",
        (
            "cn_gf01",
            "cn_gf01",
            "cn_gf01",
            "cn_gf01",
            "cn_qd01",
            "os_usa",
            "os_euro",
            "os_asia",
            "os_cht",
        ),
    )
)
SR_SERVER = {
    "1": "prod_gf_cn",
    "2": "prod_gf_cn",
    "5": "prod_qd_cn",
    **{k: v.replace("os_", "prod_official_") for k, v in SERVER.items() if k in "6789"},
}
ZZZ_SERVER = {
    "10": "prod_gf_us",
    "13": "prod_gf_jp",
    "15": "prod_gf_eu",
    "17": "prod_gf_sg",
}


@dataclass(frozen=True, slots=True)
class ApiEndpoint:
    cn: str = ""
    os: str = ""
    name: str = ""

    def get(self, is_os=False):
        region = "os" if is_os else "cn"
        url = self.os if is_os else self.cn
        if not url:
            raise ValueError(f"endpoint {self.name or '<unnamed>'} has no {region} URL")
        return url

    @property
    def has_os(self):
        return bool(self.os)

    @property
    def has_cn(self):
        return bool(self.cn)

    def format(self, *args, **kwargs):
        return self.get().format(*args, **kwargs)

    def __str__(self):
        return self.cn or self.os


def _record_endpoint(name, game, path):
    return ApiEndpoint(
        f"{RECORD_BASE}/game_record/app/{game}/api/{path}",
        f"{RECORD_BASE_OS}/game_record/{game}/api/{path}",
        name,
    )


ENDPOINTS = {
    name: _record_endpoint(name, "genshin", path)
    for name, path in (
        ("DAILY_NOTE", "dailyNote"),
        ("PLAYER_INFO", "index"),
        ("PLAYER_ABYSS_INFO", "spiralAbyss"),
        ("GCG_INFO", "gcg/basicInfo"),
        ("GCG_DECK", "gcg/deckList"),
    )
}
PUBLIC_BBS_READ_ROUTES = {
    "BBS_DETAIL": BBS_URL + "/post/api/getPostFull",
    "BBS_COLLECTION": BBS_URL + "/post/wapi/getPostFullInCollection",
}
READ_ROUTES = {
    url: (game, is_os)
    for game, endpoints in (
        ("gs", ENDPOINTS.values()),
        (
            "sr",
            [
                _record_endpoint("", "hkrpg", p)
                for p in ("note", "index", "avatar/info")
            ],
        ),
    )
    for endpoint in endpoints
    for is_os, url in ((False, endpoint.cn), (True, endpoint.os))
}


@dataclass(repr=False)
class Account:
    uid: str
    cookie: str
    # Only the host supplying the authenticated session can authorize its headers.
    # The callback receives the exact method, URL, query, body and signature mode.
    authorize: Callable | None = None
    stoken: str | None = None
    device_id: str | None = None
    fp: str | None = None
    device_info: str | None = None


_current_account = ContextVar("mys_account", default=None)


@dataclass
class _Grant:
    owner: plugin_bindings.Owner
    game: str
    account: Account
    active: bool = True
    cache: dict = field(default_factory=dict)


def _owns_task(owner):
    try:
        return owner.task is not None and owner.task is asyncio.current_task()
    except RuntimeError:
        return False


@contextmanager
def _lease(owner, game, account):
    value = _Grant(owner, game, account)
    token = _current_account.set(value)
    try:
        yield
    finally:
        value.active = False
        value.cache.clear()
        _current_account.reset(token)


@contextmanager
def account_scope(game, account):
    """Supply a trusted session for this one private owner invocation."""
    owner = plugin_bindings._current.get()
    if not owner or not owner.active or not owner.writable or not _owns_task(owner):
        raise PermissionError("a writable owner scope is required to supply an account")
    with _lease(owner, game, account):
        yield


def _account(uid, game):
    current = _current_account.get()
    owner = plugin_bindings._current.get()
    if (
        not current
        or not owner
        or not owner.active
        or not (owner.writable or owner.account_records)
        or not _owns_task(owner)
        or not current.active
        or current.owner is not owner
        or current.game != (game or "gs")
        or current.account.uid != uid
    ):
        return None
    return current.account


def _owner_models():
    owner = plugin_bindings._current.get()
    if owner and owner.active and owner.account_records and _owns_task(owner):
        return sys.modules["gsuid_core.utils.database.models"]
    return None


class GsUser:
    @classmethod
    async def get_user_attr_by_uid(cls, uid, attr, game_name=None):
        account = _account(uid, game_name)
        if account and attr in ("cookie", "stoken", "device_id", "fp"):
            return getattr(account, attr)
        return None

    @classmethod
    async def get_user_cookie_by_uid(cls, uid, game_name=None):
        return await cls.get_user_attr_by_uid(uid, "cookie", game_name)

    @classmethod
    async def get_user_stoken_by_uid(cls, uid, game_name=None):
        return await cls.get_user_attr_by_uid(uid, "stoken", game_name)

    @classmethod
    async def get_random_cookie(
        cls, uid, cache_model=None, condition=None, game_name=None
    ):
        if condition:
            game = game_name or "gs"
            key = "region" if game == "gs" else f"{game}_region"
            if condition != {key: BaseMysApi().get_server_id(uid, game)}:
                return None
        return await cls.get_user_cookie_by_uid(uid, game_name)


class BaseMysApi:
    mysVersion = "2.102.1"
    RECOGNIZE_SERVER = SERVER
    _HEADER = {
        "x-rpc-app_version": mysVersion,
        "X-Requested-With": "com.mihoyo.hyperion",
        "User-Agent": f"Mozilla/5.0 (Linux; Android 13; PHK110 Build/SKQ1.221119.001; wv)AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/126.0.6478.133 Mobile Safari/537.36 miHoYoBBS/{mysVersion}",
        "x-rpc-client_type": "5",
        "Referer": "https://webstatic.mihoyo.com/",
        "Origin": "https://webstatic.mihoyo.com/",
    }
    _HEADER_OS = {
        "x-rpc-app_version": "1.5.0",
        "x-rpc-client_type": "5",
        "x-rpc-language": "zh-cn",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/111.0.5563.116 Safari/537.36",
    }

    def check_os(self, uid, game_name="gs"):
        if game_name in ("gs", "sr"):
            return int(str(uid)[0]) >= 6
        return game_name == "zzz" and len(str(uid)) >= 10

    def get_server_id(self, uid, game_name="gs"):
        if game_name in (None, "gs"):
            return self.RECOGNIZE_SERVER.get(str(uid)[0], "cn_gf01")
        if game_name == "sr":
            return SR_SERVER.get(str(uid)[0], "prod_gf_cn")
        if game_name == "zzz" and len(uid) >= 10:
            return ZZZ_SERVER.get(uid[:2], "prod_gf_jp")
        return "prod_gf_cn"

    async def get_uid(self, uid, game_name=None):
        models = _owner_models()
        if models is not None:
            # Unset alias columns are SQL NULL; looking up None could select
            # an arbitrary role instead of proving the caller named a role.
            if type(uid) is not int and (not isinstance(uid, str) or not uid.strip()):
                raise ValueError("account queries require a role ID")
            return await models.GsUID.get_main_uid(uid, game_name)
        return uid

    async def get_ck(self, uid, mode="RANDOM", game_name=None):
        if mode not in ("RANDOM", "OWNER"):
            return None
        models = _owner_models()
        if models is not None:
            uid = await self.get_uid(uid, game_name)
            row = await models.GsUser.select_data_by_uid(uid, game_name)
            if row is None:
                return None
            if mode == "OWNER" or not row.status:
                return row.cookie
            # A fallback is only for a known role in this actor/adapter database.
            # Never consult the global cache or administrator inventory.
            owner = plugin_bindings._current.get()
            rows = await models.GsUser.select_data_list(owner.actor, owner.adapter)
            if game_name in ("gs", "sr") or (game_name == "zzz" and len(uid) >= 10):
                region = self.get_server_id(uid, game_name)
                column = "region" if game_name == "gs" else f"{game_name}_region"
                eligible = [
                    item.cookie
                    for item in rows or ()
                    if not item.status
                    and item.cookie
                    and getattr(item, column) == region
                ]
                return choice(eligible) if eligible else None
            return next(
                (
                    item.cookie
                    for item in rows or ()
                    if item.status is None and item.cookie
                ),
                None,
            )
        return await GsUser.get_user_cookie_by_uid(uid, game_name)

    async def get_stoken(self, uid, game_name=None):
        models = _owner_models()
        if models is not None:
            return await models.GsUser.get_user_stoken_by_uid(
                await self.get_uid(uid, game_name), game_name
            )
        return await GsUser.get_user_stoken_by_uid(uid, game_name)

    async def get_user_fp(self, uid, game_name=None):
        models = _owner_models()
        if models is not None:
            return await models.GsUser.get_user_attr_by_uid(
                await self.get_uid(uid, game_name), "fp", game_name
            )
        return await GsUser.get_user_attr_by_uid(uid, "fp", game_name)

    async def get_user_device_id(self, uid, game_name=None):
        models = _owner_models()
        if models is not None:
            return await models.GsUser.get_user_attr_by_uid(
                await self.get_uid(uid, game_name), "device_id", game_name
            )
        return await GsUser.get_user_attr_by_uid(uid, "device_id", game_name)

    async def simple_mys_req(
        self, endpoint, uid, params=None, header=None, cookie=None, game_name=None
    ):
        params = params or {
            "role_id": uid,
            "server": self.get_server_id(uid, game_name),
        }
        return await self.endpoint_request(
            endpoint,
            uid,
            params=params,
            header=header,
            cookie=cookie,
            cookie_mode="RANDOM",
            game_name=game_name,
        )

    async def endpoint_request(
        self,
        endpoint,
        uid,
        *,
        method="GET",
        params=None,
        data=None,
        header=None,
        cookie=None,
        cookie_mode="OWNER",
        cookie_type="cookie",
        game_name="gs",
        ds_mode="auto",
        ds_q=None,
        ds_body=None,
    ):
        game = game_name or "gs"
        account = _account(uid, game)
        if (
            cookie_type not in ("cookie", "stoken")
            or cookie_mode not in ("RANDOM", "OWNER")
            or ds_mode not in ("none", "web", "auto")
        ):
            return -51
        if account is None:
            models = _owner_models()
            if models is None:
                return -51
            canonical = await self.get_uid(uid, game)
            row = await models.GsUser.select_data_by_uid(canonical, game)
            if row is None or (cookie_mode == "RANDOM" and row.status):
                return -51
            if canonical != uid:
                # Looking up an alias can return the owner's credential, but
                # sending it for a different role requires a verified target.
                raise NotImplementedError(
                    "external record target needs its own authorization"
                )
            account = Account(
                uid,
                row.cookie,
                stoken=row.stoken,
                device_id=row.device_id,
                fp=row.fp,
                device_info=row.device_info,
            )
        credential = account.stoken if cookie_type == "stoken" else account.cookie
        if not credential or (cookie is not None and cookie != credential):
            return -51
        url = endpoint.get(self.check_os(uid, game))
        owner = plugin_bindings._current.get()
        with _lease(owner, game, account):
            return await self._send_owner(
                account,
                game,
                method,
                url,
                header,
                params,
                data,
                credential,
                ds_mode,
                ds_q,
                ds_body,
            )

    async def simple_mys_req_by_region(
        self, endpoint, is_os, params=None, header=None, cookie=None, game_name=None
    ):
        uid = (params or {}).get("role_id") or (params or {}).get("uid")
        if not _account(uid, game_name) or is_os != self.check_os(
            uid, game_name or "gs"
        ):
            return -51
        credential = cookie if cookie is not None else (header or {}).get("Cookie")
        if not credential:
            return -51
        return await self.endpoint_request(
            endpoint,
            uid,
            params=params,
            header=header,
            cookie=credential,
            game_name=game_name,
        )

    async def _mys_req_get(self, endpoint, is_os, params, header=None, game_name=None):
        uid = params.get("role_id") or params.get("uid")
        if not _account(uid, game_name) or is_os != self.check_os(
            uid, game_name or "gs"
        ):
            return -51
        return await self.endpoint_request(
            endpoint,
            uid,
            params=params,
            header=header,
            cookie=(header or {}).get("Cookie"),
            game_name=game_name,
            ds_mode="none",
        )

    async def _mys_request(
        self,
        url,
        method="GET",
        header=None,
        params=None,
        data=None,
        use_proxy=False,
        base_url=None,
        game_name=None,
        time_out=300,
    ):
        uid = (params or {}).get("role_id") or (params or {}).get("uid")
        account = _account(uid, game_name)
        if not account:
            return -51
        credential = (header or {}).get("Cookie")
        if not credential or credential not in (account.cookie, account.stoken):
            return -51
        return await self._send_owner(
            account,
            game_name or "gs",
            method,
            (base_url or "") + url,
            header,
            params,
            data,
            credential,
            "none",
            None,
            None,
            time_out,
        )

    async def _send_owner(
        self,
        account,
        game,
        method,
        url,
        header,
        params,
        data,
        credential,
        ds_mode,
        ds_q,
        ds_body,
        timeout=300,
    ):
        if (
            account is not _account(account.uid, game)
            or not credential
            or credential not in (account.cookie, account.stoken)
        ):
            return -51
        if method != "GET" or data or url not in READ_ROUTES:
            raise NotImplementedError("unsupported account record endpoint")
        if READ_ROUTES[url] != (game, self.check_os(account.uid, game)):
            return -51
        params = dict(params or {})
        owner_ids = [params[k] for k in ("uid", "role_id") if k in params]
        if not owner_ids or any(uid != account.uid for uid in owner_ids):
            return -51
        if "server" in params and params["server"] != self.get_server_id(
            account.uid, game
        ):
            return -51
        headers = dict(
            self._HEADER_OS if self.check_os(account.uid, game) else self._HEADER
        )
        if account.device_id:
            headers["x-rpc-device_id"] = account.device_id
        if account.fp:
            headers["x-rpc-device_fp"] = account.fp
        if account.device_info is not None:
            device = account.device_info.split("/")
            headers["User-Agent"] = (
                f"Mozilla/5.0 (Linux; Android 13; {device[1]} {device[3]} "
                "; wv)AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Version/4.0 Chrome/104.0.5112.97Mobile Safari/537.36 miHoYoBBS/2{self.mysVersion}"
            )
        if ds_mode != "none" or (header and any(key.lower() == "ds" for key in header)):
            if not account.authorize:
                raise NotImplementedError("account request authorization unavailable")
            supplied = account.authorize(
                method, url, params.copy(), data, ds_mode, ds_q, ds_body
            )
            if not supplied or not any(
                k.lower() == "ds" and v for k, v in supplied.items()
            ):
                return -51
            headers.update({k: v for k, v in supplied.items() if k.lower() != "cookie"})
        # A plugin may repeat the trusted identity, but cannot substitute it.
        accepted = {k.lower(): v for k, v in headers.items()}
        accepted["cookie"] = credential
        for key, value in (header or {}).items():
            if key.lower() in ("accept", "content-type"):
                headers[key] = value
            elif key.lower() not in accepted or accepted[key.lower()] != value:
                return -51
        headers["Cookie"] = credential
        params = {
            k: str(v).lower() if isinstance(v, bool) else v for k, v in params.items()
        }
        for _ in range(2):
            if account is not _account(account.uid, game):
                return -51
            try:
                complete_json, raw = await _read_response(
                    url, headers, params, timeout, owner=True
                )
            except _ConnectionNotEstablished:
                message = "[mys_request] 请求连接错误..."
                fields = {}
            else:
                if account is not _account(account.uid, game):
                    return -51
                if complete_json:
                    raw = (
                        raw.strip()
                    )  # aiohttp.ClientResponse.json strips the wire body
                    if not raw:
                        raise ValueError("empty account API response")
                try:
                    return _decode_response(raw)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    if not complete_json:
                        raise ValueError("invalid account error response") from None
                    # A completed body failed local JSON parsing. Only disclose
                    # parser coordinates and reviewed constant diagnostics.
                    detail = "invalid JSON response"
                    if isinstance(exc, json.JSONDecodeError) and exc.msg in (
                        "Expecting value",
                        "Expecting property name enclosed in double quotes",
                        "Expecting ':' delimiter",
                        "Expecting ',' delimiter",
                        "Extra data",
                        "Unterminated string starting at",
                    ):
                        detail = f"{exc.msg}: line {exc.lineno} column {exc.colno} (char {exc.pos})"
                    message = (
                        "[mys_request] 请求错误, 请联系Bot主人检查控制台! 错误信息: {e}"
                    )
                    fields = {"e": detail}
            if account is not _account(account.uid, game):
                return -51
            from gsuid_core.bot import call_bot

            bot = call_bot()
            await bot.send(await bot.t(message, **fields))
        return -999


class _ConnectionNotEstablished(Exception):
    """The transport could not resolve or connect before sending the GET."""


def _known_unconnected(exc):
    import errno
    import socket

    # Only a concrete pre-connect OS cause is sufficient. A bare client error,
    # policy rejection, TLS failure or timeout gives us no retry authority.
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, (ConnectionRefusedError, socket.gaierror)) or (
            type(exc) is OSError
            and exc.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH)
        ):
            return True
        if isinstance(exc, (PermissionError, TimeoutError)):
            break
        exc = exc.__cause__
    return False


async def _read_response(url, headers, params, timeout=300, *, owner=False):
    from httpx2 import AsyncClient, ConnectError, TransportError

    async with AsyncClient(
        follow_redirects=False, trust_env=False, timeout=timeout
    ) as client:
        received_headers = False
        try:
            async with client.stream(
                "GET", url, headers=headers, params=params
            ) as response:
                received_headers = True
                media = (
                    response.headers.get("Content-Type", "")
                    .partition(";")[0]
                    .strip()
                    .lower()
                )
                complete_json = (
                    response.status_code == 200
                    and "Content-Range" not in response.headers
                    and (
                        media == "application/json"
                        or (
                            media.startswith("application/") and media.endswith("+json")
                        )
                    )
                    and (response.encoding or "utf-8").lower() in ("utf-8", "utf8")
                )
                raw = bytearray()
                async for part in response.aiter_bytes(chunk_size=65536):
                    if len(raw) + len(part) > 8 * 1024 * 1024:
                        raise ValueError("account API response exceeds size limit")
                    raw.extend(part)
        except TransportError as exc:
            if not owner:
                raise
            if (
                isinstance(exc, ConnectError)
                and not received_headers
                and _known_unconnected(exc)
            ):
                raise _ConnectionNotEstablished from None
            raise type(exc)("account transport unavailable") from None
        except (PermissionError, TimeoutError) as exc:
            if not owner:
                raise
            raise type(exc)("account transport unavailable") from None
    return (complete_json, raw) if owner else raw


def _decode_response(raw):
    result = json.loads(raw)
    if "retcode" in result:
        code = result["retcode"]
    elif "code" in result:
        code = result["code"]
    else:
        return result
    return result if code == 0 else code


class MysApi(BaseMysApi):
    _authorize_public_bbs = None

    async def _public_bbs(self, name, params):
        if not self._authorize_public_bbs:
            raise NotImplementedError("public BBS web signature issuer unavailable")
        url = PUBLIC_BBS_READ_ROUTES[name]
        signature = self._authorize_public_bbs("GET", url, dict(params))
        if not isinstance(signature, str) or not signature:
            raise PermissionError("public BBS signature unavailable")
        return _decode_response(await _read_response(url, {"DS": signature}, params))

    async def get_bbs_post_detail(self, post_id):
        result = await self._public_bbs("BBS_DETAIL", {"post_id": post_id})
        return result["data"] if isinstance(result, dict) else result

    async def get_bbs_collection(self, collection_id, gids="2"):
        result = await self._public_bbs(
            "BBS_COLLECTION",
            {"collection_id": collection_id, "gids": gids, "order_type": 1},
        )
        if isinstance(result, dict):
            if "data" in result and "posts" in result["data"]:
                return result["data"]["posts"]
            return -500
        return result

    async def _record(self, name, uid, cookie=None, params=None):
        result = await self.simple_mys_req(
            ENDPOINTS[name], uid, params=params, cookie=cookie, game_name="gs"
        )
        return result["data"] if isinstance(result, dict) else result

    async def _cached_record(self, name, uid, ck, params=None):
        account = _account(uid, "gs")
        if account is None and _owner_models() is not None:
            return await self._record(name, uid, ck, params)
        if not account or (ck is not None and ck != account.cookie):
            return -51
        cache = _current_account.get().cache
        key = (name, ck is not None, tuple((params or {}).items()))
        cached = cache.get(key)
        if cached and time.monotonic() < cached[0]:
            return cached[1]
        result = await self._record(name, uid, ck, params)
        if result is not None:
            cache[key] = (time.monotonic() + 360, result)
        return result

    async def get_info(self, uid, ck=None):
        return await self._cached_record("PLAYER_INFO", uid, ck)

    async def get_spiral_abyss_info(self, uid, schedule_type="1", ck=None):
        return await self._cached_record(
            "PLAYER_ABYSS_INFO",
            uid,
            ck,
            {
                "role_id": uid,
                "schedule_type": schedule_type,
                "server": self.get_server_id(uid, "gs"),
            },
        )

    async def get_daily_data(self, uid):
        return await self._record("DAILY_NOTE", uid)

    async def get_gcg_info(self, uid):
        return await self._record("GCG_INFO", uid)

    async def get_gcg_deck(self, uid):
        return await self._record("GCG_DECK", uid)


def install_mys(package):
    prefix = "gsuid_core.utils.api.mys"
    api = package(prefix)
    # This directory contains the pinned type and endpoint declarations.
    # Genshin-bots/gsuid_core 96cd7353, GPL-3.0 (licenses/gsuid_core-COPYING.txt).
    api.__path__.append(str(Path(__file__).parent / "plugin_mys_schema"))
    models = import_module(prefix + ".models")
    api.models = models
    for name in (
        "AbyssData",
        "IndexData",
        "MihoyoRole",
        "MihoyoAvatar",
        "MihoyoWeapon",
        "DailyNoteData",
        "MihoyoReliquary",
        "MihoyoConstellation",
    ):
        setattr(api, name, getattr(models, name))
    api.MysApi = MysApi
    api.__all__ = ["models", "request"]
    # Publishing an endpoint describes its URL. Request admission stays in
    # _send_owner and does not use this public namespace as an allowlist.
    import_module(prefix + ".api")
    base = package(prefix + ".base_request")
    base.BaseMysApi = BaseMysApi
    base.RECOGNIZE_SERVER = SERVER
    package(prefix + ".request").MysApi = MysApi
    signing = package(prefix + ".tools")
    signing.mys_version = BaseMysApi.mysVersion
    # Package modules import these eagerly, even for tasks that never sign.
    # Only a complete, owner-bound request can receive host authorization.
    signing.get_ds_token = signing.get_web_ds_token = signing.generate_os_ds = (
        _unbound_signature
    )
    legacy = package("gsuid_core.utils.api.mys_api")
    legacy._MysApi = MysApi
    legacy.mys_api = MysApi()
    sys.modules["gsuid_core.utils.database.models"].GsUser = GsUser


def _unbound_signature(*args, **kwargs):
    raise NotImplementedError("standalone account signatures require a trusted issuer")
