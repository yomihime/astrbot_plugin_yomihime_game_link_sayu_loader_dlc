"""Image HTTP responses and URL cache for one private plugin profile."""

import asyncio
import hashlib
import os
from io import BytesIO
from pathlib import Path
import tempfile
import time

from port_errors import Unsupported


def install_image_fetch(package, data_root, *, transport=None, published=None):
    """Bind a controlled transport, or use the immutable source's public image manifest."""
    import httpx2
    from PIL import Image
    from sayu_compat import MAX_INPUT_IMAGE_BYTES, MAX_INPUT_PIXELS

    if transport is None and published is not None:

        class ImageTransport(httpx2.AsyncBaseTransport):
            async def handle_async_request(self, request):
                try:
                    status, headers, body = await asyncio.to_thread(
                        published.read, str(request.url)
                    )
                except TimeoutError as exc:
                    raise httpx2.ReadTimeout(
                        "published image timed out", request=request
                    ) from exc
                except OSError as exc:
                    raise httpx2.ConnectError(
                        "public image fetch failed", request=request
                    ) from exc
                return httpx2.Response(
                    status, headers=headers, content=body, request=request
                )

        transport = ImageTransport()

    root = Path(data_root).resolve()
    cache = root / "resources" / "IMAGE_CACHE" / "sget"

    def cached_path(url):
        path = cache / (hashlib.sha256(url.encode("utf8")).hexdigest() + ".bin")
        if path.resolve() != path or (
            path.exists() and (not path.is_file() or path.stat().st_nlink != 1)
        ):
            raise Unsupported("image cache outside private storage")
        return path

    def read_cache(url):
        path = cached_path(url)
        try:
            if time.time() - path.stat().st_mtime > 172800:
                return None
            with path.open("rb") as stream:
                raw = stream.read(MAX_INPUT_IMAGE_BYTES + 1)
        except FileNotFoundError:
            return None
        if len(raw) > MAX_INPUT_IMAGE_BYTES:
            raise Unsupported("image cache entry exceeds the worker limit")
        return raw

    def write_cache(url, content):
        path = cached_path(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            pending = Path(stream.name)
        try:
            pending.write_bytes(content)
            os.replace(pending, path)
        finally:
            pending.unlink(missing_ok=True)

    async def sget(url, use_cache=False):
        if use_cache:
            hit = await asyncio.to_thread(read_cache, url)
            if hit is not None:
                return httpx2.Response(
                    200, content=hit, request=httpx2.Request("GET", url)
                )
        if transport is None:
            raise Unsupported("image fetch requires a permitted HTTP transport")
        timeout = httpx2.Timeout(connect=3, read=8, write=3, pool=3)
        async with (
            asyncio.timeout(12),
            httpx2.AsyncClient(
                transport=transport,
                trust_env=False,
                follow_redirects=True,
                timeout=timeout,
            ) as client,
        ):
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > MAX_INPUT_IMAGE_BYTES:
                        raise Unsupported("image response exceeds the worker limit")
                    content.extend(chunk)
                # httpx2 exposes a completed Response to callers of get(). Build its
                # body from decoded chunks before restoring its original headers.
                result = httpx2.Response(
                    response.status_code,
                    content=bytes(content),
                    request=response.request,
                    history=response.history,
                    extensions=response.extensions,
                )
                result.headers = response.headers.copy()
            # Mock and already-consumed streams can legitimately have no
            # transport timing (the same Response property then raises).
            if hasattr(response, "_elapsed"):
                result.elapsed = response.elapsed
        if use_cache:
            await asyncio.to_thread(write_cache, url, result.content)
        return result

    async def download_pic_to_image(url):
        result = await sget(url)
        image = Image.open(BytesIO(result.content))
        if image.width * image.height > MAX_INPUT_PIXELS:
            image.close()
            raise Unsupported("image dimensions exceed the worker limit")
        return image

    module = package("gsuid_core.utils.image.utils")
    module.sget = sget
    module.download_pic_to_image = download_pic_to_image
    return module
