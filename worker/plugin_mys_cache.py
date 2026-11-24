"""Sync and async gs_cache decorators for profile-local Sayu packages."""

from base64 import b64decode, b64encode
from functools import wraps
import hashlib
import inspect
from io import BytesIO
import json
from pathlib import Path
import time

import plugin_bindings


def _argument_key(args, kwargs):
    parts = []
    for arg in (*args, *kwargs.values()):
        if isinstance(arg, (str, int, float, tuple, Path)):
            parts.append(repr(arg))
        elif isinstance(arg, (dict, list)):
            parts.append(json.dumps(arg, sort_keys=isinstance(arg, dict)))
    return hashlib.sha256(json.dumps(parts).encode()).digest()


def _for_hit(value):
    from PIL import Image

    if isinstance(value, Image.Image):
        buffer = BytesIO()
        value.save(buffer, format="JPEG")
        value = buffer.getvalue()
    elif isinstance(value, str) and value.startswith("base64://"):
        value = b64decode(value[9:])
    if isinstance(value, bytes):
        return "base64://" + b64encode(value).decode("ascii")
    return value


def cache_decorator(expire_time=3600):
    def decorate(func):
        def lookup(args, kwargs):
            from plugin_mys import _current_account, _owns_task

            owner = plugin_bindings._current.get()
            if not owner or not owner.active or not _owns_task(owner):
                return None, None, None
            grant = _current_account.get()
            if grant is not None and (
                not grant.active or not owner.writable or grant.owner is not owner
            ):
                return None, None, None
            # Keep data on the active delivery/credential lease. Disposing the lease
            # makes the entries unreachable to later or background invocations.
            store = grant.cache if grant else vars(owner).setdefault("_plugin_cache", {})
            key = (func, _argument_key(args, kwargs))
            entry = store.get(key)
            if entry is not None and entry[0] >= time.monotonic():
                return store, key, entry[1]
            store.pop(key, None)
            return store, key, None

        def save(store, key, value):
            if store is not None and value is not None:
                store[key] = (time.monotonic() + expire_time, _for_hit(value))
            return value

        if inspect.iscoroutinefunction(func):

            @wraps(func)
            async def async_call(*args, **kwargs):
                store, key, value = lookup(args, kwargs)
                if value is not None:
                    if isinstance(value, Path):
                        from gsuid_core.utils.image.convert import convert_img

                        return await convert_img(value)
                    return value
                return save(store, key, await func(*args, **kwargs))

            return async_call

        @wraps(func)
        def sync_call(*args, **kwargs):
            store, key, value = lookup(args, kwargs)
            if value is not None:
                if isinstance(value, Path):
                    from gsuid_core.utils.image.convert import convert_img_sync

                    return convert_img_sync(value)
                return value
            return save(store, key, func(*args, **kwargs))

        return sync_call

    return decorate


def install_cache(package):
    from gsuid_core.data_store import get_res_path

    module = package("gsuid_core.utils.cache")
    module.gs_cache = cache_decorator
    module.IMAGE_CACHE = get_res_path("IMAGE_CACHE")
