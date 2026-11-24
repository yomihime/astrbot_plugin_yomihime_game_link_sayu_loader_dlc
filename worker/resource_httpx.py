"""HTTPX transport for published, unauthenticated assets in a resource-only worker."""

import asyncio
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
from time import monotonic

from port_errors import Unsupported


_streams = ContextVar("static_http_streams", default=None)


@contextmanager
def stream_scope():
    """A retired callback cannot keep a stream or start another from a copied context."""

    class Lease:
        active = True

    lease = Lease()
    lease.live = set()
    token = _streams.set(lease)
    try:
        yield
    finally:
        lease.active = False
        for response in tuple(lease.live):
            response.close()
        _streams.reset(token)


@contextmanager
def static_httpx(profile, reader):
    import httpx
    from resource_broker import (
        MAX_IMAGE,
        allowed,
        ambr_asset,
        stream_spec,
        transfer_window,
    )

    reads = {}
    if getattr(profile, "MODULE_ROUTE", None):
        from anonymous_sources import SOURCES, target

        reads = {target(spec): source for source, spec in SOURCES.get(profile.MODULE_ROUTE, {}).items()}
    original = httpx.AsyncClient
    # Use the pinned client's own default headers, including its installed codecs.
    # An explicitly supplied transport keeps this unconnected client pool-free.
    defaults = original(transport=httpx.AsyncBaseTransport(), trust_env=False).headers
    records = reader.static_httpx = deque(maxlen=32)
    objects = {url for url, _, _ in getattr(profile, "STATIC_STREAM_OBJECTS", ())}

    class StaticBody(httpx.AsyncByteStream):
        def __init__(self, lease, transport, url, record, size):
            self.lease = lease
            self.transport = transport
            self.record = record
            self.size = size
            self.closed = False
            self.window = transfer_window()
            deadline = self.window.__enter__()
            try:
                self.pipe = reader.stream(
                    url, min(deadline or float("inf"), monotonic() + 15)
                )
            except BaseException:
                self.window.__exit__(None, None, None)
                raise
            lease.live.add(self)
            transport.live.add(self)
            record.update(chunks=0, bytes=0)

        async def __aiter__(self):
            while self.lease.active and not self.closed:
                try:
                    status, _, data = await self.pipe.next()
                except BaseException as exc:
                    self.record["error"] = type(exc).__name__
                    raise
                if self.closed or not self.lease.active:
                    break
                if status == "eof":
                    if (
                        self.record["status"] == 200
                        and self.record["bytes"] != self.size
                    ):
                        raise Unsupported("incomplete public stream")
                    self.record["eof"] = True
                    return
                if (
                    status != "chunk"
                    or not 0 < len(data) <= 65536
                    or self.record["bytes"] + len(data) > self.size
                ):
                    raise Unsupported("invalid public stream frame")
                self.record["chunks"] += 1
                self.record["bytes"] += len(data)
                yield data
            raise Unsupported("static stream callback retired")

        def close(self):
            if self.closed:
                return
            self.closed = True
            self.lease.live.discard(self)
            self.transport.live.discard(self)
            self.pipe.cancel()
            self.window.__exit__(None, None, None)
            self.record["closed"] = True
            records.append(self.record)

        async def aclose(self):
            self.close()

    class StaticTransport(httpx.AsyncBaseTransport):
        def __init__(self, blocked):
            self.blocked = blocked
            self.live = set()

        async def aclose(self):
            for body in tuple(self.live):
                body.close()

        async def handle_async_request(self, request):
            url = str(request.url)
            if (
                self.blocked
                or request.method != "GET"
                or (
                    url not in reads
                    and url not in objects
                    and url not in profile.STATIC_RESOURCE_URLS
                    and url not in getattr(profile, "PUBLISHED_IMAGE_DIGESTS", {})
                    and not ambr_asset(url)
                )
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
            if url in reads:
                # This marked request never enters the static asset broker.
                from anonymous_http import get

                return httpx.Response(200, content=await get(reads[url], request.extensions.get("timeout", {})))
            record = {
                "origin": request.url.host,
                "asset_sha256": sha256(url.encode()).hexdigest(),
            }
            if url in objects:
                lease = _streams.get()
                if lease is None or not lease.active:
                    raise Unsupported("public stream needs a current callback")
                _, size = stream_spec(url, profile)
                body = StaticBody(lease, self, url, record, size)
                try:
                    status, headers, data = await body.pipe.next()
                    if (
                        type(status) is not int
                        or not 100 <= status <= 599
                        or data
                        or body.closed
                    ):
                        raise Unsupported("invalid public stream headers")
                    record["status"] = status
                    return httpx.Response(status, headers=headers, stream=body)
                except BaseException as exc:
                    record["error"] = type(exc).__name__
                    body.close()
                    raise
            allowed(url, profile)
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
