"""Bounded transport for the pinned GsCore public game-data providers."""

from pathlib import Path
from functools import wraps
from importlib import import_module
import sys
from urllib.parse import urlsplit


HEADER = {"User-Agent": "gsuid-utils/0.12.2"}
MAX_BODY = 8 * 1024 * 1024


async def public_get(url, *, params=None):
    from httpx2 import AsyncClient, Response

    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc not in {
        "akashadata.feixiaoqiu.com",
        "gi.yatta.moe",
        "enka.network",
        "genshin.honeyhunterworld.com",
    }:
        raise ValueError("unsupported public API origin")
    async with AsyncClient(
        follow_redirects=False, timeout=30, trust_env=False
    ) as client:
        async with client.stream("GET", url, headers=HEADER, params=params) as response:
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=65536):
                if len(body) + len(chunk) > MAX_BODY:
                    raise ValueError("public API response exceeds size limit")
                body.extend(chunk)
            # Streamed content is already decoded by HTTPX.
            return Response(
                response.status_code,
                headers={"Content-Type": response.headers.get("content-type", "")},
                content=bytes(body),
            )


def install_public_api():
    # Pure type declarations and constants from Genshin-bots/gsuid_core at
    # 96cd7353be3100ff982bccc33e554961837c778e (licenses/gsuid_core-COPYING.txt).
    api = sys.modules["gsuid_core.utils.api"]
    directory = str(Path(__file__).parent / "plugin_public_ports")
    if directory not in api.__path__:
        api.__path__.append(directory)
    minigg = sys.modules.get("gsuid_core.utils.api.minigg")
    if minigg is not None:
        minigg.__path__.append(str(Path(directory) / "minigg"))
        models = import_module(minigg.__name__ + ".models")
        for name in (
            "Food",
            "Costs",
            "Enemy",
            "Domain",
            "Weapon",
            "Artifact",
            "Material",
            "Character",
            "WeaponStats",
            "CharacterStats",
            "CharacterTalents",
            "CharacterConstellations",
        ):
            setattr(minigg, name, getattr(models, name))
        minigg.__all__ = ["request", "exception", "models"]


def cache_data(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        from gsuid_core.data_store import get_res_path
        from plugin_minigg import MiniGG, cached_json

        identifier = (
            kwargs.get("id", kwargs.get("name", args[0] if args else function.__name__))
            or function.__name__
        )
        path = MiniGG(get_res_path()).cache_file(
            kwargs.get("cache_path"), function.__name__, identifier
        )
        return await cached_json(path, function, *args, **kwargs)

    return wrapped
