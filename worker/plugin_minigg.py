"""MiniGG public queries and per-profile response cache used by gsuid_core plugins."""

import asyncio
import functools
import json
import os
import re
import tempfile
import warnings
from enum import Enum
from pathlib import Path, PureWindowsPath
from weakref import WeakValueDictionary


class APILanguages(str, Enum):
    CHS = CN = "CN"
    JP = JA = "JP"
    EN = ENG = "EN"
    KR = KA = "KR"
    CHT = "CHT"
    FR = "FR"
    DE = "DE"
    ID = "ID"
    PT = "PT"
    RU = "RU"
    ES = "ES"
    TH = "TH"
    VI = "VI"


class MiniggNotFoundError(Exception):
    def __init__(self, **raw):
        self.raw = raw

    def __repr__(self):
        return f"<MiniggNotFoundError raw={self.raw!r}>"

    __str__ = __repr__


CN = APILanguages.CHS
MAX_BYTES = 8 * 1024 * 1024
FUNCTIONS = (
    "get_map_data",
    "get_audio_info",
    "get_weapon_info",
    "get_weapon_costs",
    "get_weapon_stats",
    "get_character_info",
    "get_character_costs",
    "get_character_stats",
    "get_constellation_info",
    "get_talent_info",
    "get_others_info",
)
REQUEST_EXPORTS = (*FUNCTIONS, "minigg_request", "APILanguages", "MiniggNotFoundError")
_CACHE_LOCKS = WeakValueDictionary()


def _read_cache(path):
    try:
        with path.open("rb") as source:
            data = source.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return False, None
    if len(data) > MAX_BYTES:
        raise ValueError("MiniGG cache entry exceeds size limit")
    return True, json.loads(data)


def _write_cache(path, result):
    raw = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
    if len(raw) > MAX_BYTES:
        raise ValueError("MiniGG cache entry exceeds size limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = tempfile.NamedTemporaryFile(dir=path.parent, delete=False)
    temp = Path(stream.name)
    try:
        with stream:
            stream.write(raw)
        try:
            if os.name == "nt":
                # On Windows rename publishes only if the destination is absent.
                temp.rename(path)
            else:
                os.link(temp, path)
        except (FileExistsError, PermissionError):
            # Another process may already have published and opened the entry.
            # Never replace a completed cache entry currently being read.
            hit, existing = _read_cache(path)
            if not hit:
                raise
            return existing
        return result
    finally:
        temp.unlink(missing_ok=True)


def _cached(function):
    @functools.wraps(function)
    async def wrapped(self, *args, **kwargs):
        # Native cache_data keys the result by function and name only, including
        # when the next request chooses different query/result languages.
        name = (
            kwargs.get("name", args[0] if args else function.__name__)
            or function.__name__
        )
        path = self.cache_file(kwargs.get("cache_path"), function.__name__, name)
        return await cached_json(path, function, self, *args, **kwargs)

    return wrapped


async def cached_json(path, function, *args, **kwargs):
    key = (asyncio.get_running_loop(), path)
    lock = _CACHE_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        hit, result = await asyncio.to_thread(_read_cache, path)
        if hit:
            return result
        result = await function(*args, **kwargs)
        return await asyncio.to_thread(_write_cache, path, result)


def _record(result):
    return result if isinstance(result, (int, dict)) else -1


def _level(stats):
    if stats > 90 or stats <= 0:
        raise ValueError("stats must <= 90 and > 0")
    return stats


class MiniGG:
    def __init__(self, resource_root, transport=None):
        self.root = Path(resource_root).resolve()
        self.transport = transport

    def cache_file(self, directory, function, name):
        directory = Path(directory) if directory else self.root / "DATA_CACHE_PATH"
        leaf = f"{name}.json"
        if (
            not name
            or name in (".", "..")
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', str(name))
            or PureWindowsPath(leaf).is_reserved()
        ):
            raise ValueError("invalid MiniGG cache key")
        target = (directory / function / leaf).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError("MiniGG cache outside profile resources")
        return target

    async def _get(self, url, params, timeout):
        from httpx2 import AsyncClient

        async with AsyncClient(
            transport=self.transport, follow_redirects=False, timeout=timeout
        ) as client:
            async with client.stream("GET", url, params=params) as response:
                body = bytearray()
                async for part in response.aiter_bytes(chunk_size=65536):
                    if len(body) + len(part) > MAX_BYTES:
                        raise ValueError("MiniGG response exceeds size limit")
                    body.extend(part)
                return response, bytes(body)

    async def minigg_request(
        self,
        endpoint,
        query,
        query_languages=CN,
        result_languages=CN,
        match_categories=False,
        **kwargs,
    ):
        if not re.fullmatch(r"/?[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*/?", endpoint):
            raise ValueError("expected a MiniGG API endpoint path")
        params = {
            "query": query,
            "queryLanguages": query_languages.value,
            "resultLanguage": result_languages.value,
            **kwargs,
        }
        if match_categories:
            params["matchCategories"] = "1"
        response, body = await self._get(
            "https://info.minigg.cn/" + endpoint.lstrip("/"), params, 1.3
        )
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return -11
        if "retcode" in data:
            return data["retcode"]
        if response.status_code == 404:
            raise MiniggNotFoundError(**data)
        if response.status_code == 502:
            raise Exception("Minigg API is unavailable.")
        if response.status_code != 200:
            raise Exception(
                f"Minigg API is unavailable. (Error code: {response.status_code})"
            )
        return data

    async def get_map_data(self, resource_name, map_id, is_cluster=False):
        response, body = await self._get(
            "https://map.minigg.cn/map/get_map",
            {
                "resource_name": resource_name,
                "map_id": map_id,
                "is_cluster": is_cluster,
            },
            None,
        )
        if response.headers["content-type"] == "image/jpeg":
            return body
        raise MiniggNotFoundError(**json.loads(body))

    async def get_audio_info(self, name, audio_id, language="cn"):
        warnings.warn(
            "Audio API is already deprecated.", DeprecationWarning, stacklevel=2
        )
        response, body = await self._get(
            "https://genshin.minigg.cn/",
            {"characters": name, "audioid": audio_id, "language": language},
            None,
        )
        return body.decode(response.encoding or "utf8", errors="replace")

    @_cached
    async def get_weapon_info(
        self, name, query_languages=CN, result_languages=CN, cache_path=None
    ):
        return await self.minigg_request(
            "/weapons", name, query_languages, result_languages
        )

    async def get_weapon_costs(self, name, query_languages=CN, result_languages=CN):
        return await self.minigg_request(
            "/weapons", name, query_languages, result_languages, costs=True
        )

    async def get_weapon_stats(
        self, name, stats, query_languages=CN, result_languages=CN
    ):
        return await self.minigg_request(
            "/weapons", name, query_languages, result_languages, stats=_level(stats)
        )

    @_cached
    async def get_character_info(
        self, name, query_languages=CN, result_languages=CN, cache_path=None
    ):
        return await self.minigg_request(
            "/characters", name, query_languages, result_languages
        )

    @_cached
    async def get_character_costs(
        self, name, query_languages=CN, result_languages=CN, cache_path=None
    ):
        result = await self.minigg_request(
            "/characters", name, query_languages, result_languages, Costs=True
        )
        return result["costs"] if isinstance(result, dict) else _record(result)

    async def get_character_stats(
        self, name, stats, query_languages=CN, result_languages=CN
    ):
        return _record(
            await self.minigg_request(
                "/characters",
                name,
                query_languages,
                result_languages,
                stats=_level(stats),
            )
        )

    async def get_constellation_info(
        self, name, c=None, query_languages=CN, result_languages=CN
    ):
        if c and (c > 6 or c <= 0):
            raise ValueError("c must <= 6 and > 0")
        return _record(
            await self.minigg_request(
                "/constellations", name, query_languages, result_languages, c=c
            )
        )

    @_cached
    async def get_talent_info(
        self, name, query_languages=CN, result_languages=CN, cache_path=None
    ):
        return _record(
            await self.minigg_request(
                "/talents", name, query_languages, result_languages
            )
        )

    async def get_others_info(
        self, type, name, query_languages=CN, result_languages=CN
    ):
        result = await self.minigg_request(
            f"/{type}", name, query_languages, result_languages
        )
        if isinstance(result, dict) and type not in (
            "foods",
            "enemies",
            "domains",
            "artifacts",
            "materials",
        ):
            return None
        return _record(result)


def install_minigg(package, data_root):
    package("gsuid_core.utils.api")
    api = package("gsuid_core.utils.api.minigg")
    request = package(api.__name__ + ".request")
    exception = package(api.__name__ + ".exception")
    exception.MiniggNotFoundError = api.MiniggNotFoundError = (
        request.MiniggNotFoundError
    ) = MiniggNotFoundError
    request.APILanguages = APILanguages
    profile = Path(data_root).resolve()
    resources = profile / "resources"
    if resources.resolve() != resources:
        raise ValueError("unexpected MiniGG resource root")
    port = MiniGG(resources)
    request.minigg_request = port.minigg_request
    for name in FUNCTIONS:
        setattr(request, name, getattr(port, name))
        setattr(api, name, getattr(port, name))
