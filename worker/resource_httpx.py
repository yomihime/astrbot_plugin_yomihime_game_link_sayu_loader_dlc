"""HTTPX transport for published, unauthenticated assets in a resource-only worker."""

import asyncio
from collections import deque
from contextlib import contextmanager
from hashlib import sha256

from port_errors import Unsupported


@contextmanager
def static_httpx(profile, reader):
    import httpx
    from resource_broker import MAX_IMAGE, allowed, ambr_asset

    original = httpx.AsyncClient
    # Use the pinned client's own default headers, including its installed codecs.
    # An explicitly supplied transport keeps this unconnected client pool-free.
    defaults = original(transport=httpx.AsyncBaseTransport(), trust_env=False).headers
    records = reader.static_httpx = deque(maxlen=32)

    class StaticTransport(httpx.AsyncBaseTransport):
        def __init__(self, blocked):
            self.blocked = blocked

        async def handle_async_request(self, request):
            url = str(request.url)
            if (
                self.blocked
                or request.method != "GET"
                or (url not in profile.STATIC_RESOURCE_URLS and not ambr_asset(url))
                or request.headers.get("host") != request.url.host
            ):
                raise Unsupported("HTTP request is outside the static resource profile")
            expected = httpx.Headers(defaults)
            expected["host"] = request.url.host
            if request.headers != expected:
                raise Unsupported("only anonymous default HTTP headers are supported")
            try:
                body = request.content
            except httpx.RequestNotRead as exc:
                raise Unsupported("streamed resource requests are unavailable") from exc
            if body:
                raise Unsupported("resource GET must not contain a body")
            allowed(url, profile)
            record = {
                "origin": request.url.host,
                "asset_sha256": sha256(url.encode()).hexdigest(),
            }
            try:
                status, headers, data = await asyncio.to_thread(
                    reader.read, url, MAX_IMAGE
                )
            except Exception as exc:
                records.append({**record, "error": type(exc).__name__})
                raise
            records.append({**record, "status": status, "bytes": len(data)})
            return httpx.Response(status, headers=headers, content=data)

    class ResourceClient(original):
        def __init__(self, *args, **kwargs):
            blocked = (
                any(
                    kwargs.get(name) is not None
                    for name in ("transport", "proxy", "mounts", "cert", "auth")
                )
                or kwargs.get("verify", True) is not True
            )
            kwargs.update(
                transport=StaticTransport(blocked),
                proxy=None,
                mounts=None,
                trust_env=False,
                auth=None,
            )
            super().__init__(*args, **kwargs)

    httpx.AsyncClient = ResourceClient
    try:
        yield
    finally:
        httpx.AsyncClient = original
