"""Profile-local downloads from public static origins used by the pinned packages."""

import asyncio
from io import BytesIO
import os
import re
import ssl
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import unquote, urlsplit

from PIL import Image, UnidentifiedImageError

from port_errors import Unsupported
from plugin_resource_mirror import Mirror, segment

EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
MAX_BYTES = 8 * 1024 * 1024


def _size(size):
    from sayu_compat import MAX_INPUT_PIXELS

    if (
        len(size) != 2
        or any(type(n) is not int or n <= 0 for n in size)
        or size[0] * size[1] > MAX_INPUT_PIXELS
    ):
        raise Unsupported("invalid static image dimensions")


def _origin(base, *, mirror=False):
    p = urlsplit(base)
    host = p.netloc.lower()
    # Hosts are issued by the pinned profile, never by a caller or mirror index.
    if (
        p.scheme != "https"
        or not re.fullmatch(r"(?:[a-z0-9-]+\.)+[a-z][a-z0-9-]+", host)
        or host.endswith((".local", ".localhost", ".internal", ".localdomain"))
        or p.query
        or p.fragment
        or (not mirror and p.path.strip("/"))
    ):
        raise Unsupported("invalid public resource origin")
    for chunk in p.path.strip("/").split("/"):
        if chunk:
            segment(unquote(chunk))
    return "https://" + host + (p.path.rstrip("/") if mirror else "")


def _public_url(url, origins):
    if not isinstance(url, str) or len(url) > 2048:
        raise Unsupported("invalid static resource URL")
    parsed = urlsplit(url)
    path = unquote(parsed.path)
    if (
        parsed.scheme != "https"
        or "https://" + parsed.netloc.lower() not in origins
        or parsed.query
        or parsed.fragment
        or "\\" in path
        or any(ord(ch) < 32 for ch in path)
        or any(p in (".", "..") for p in path.split("/"))
        or Path(path).suffix.lower() not in EXTENSIONS
    ):
        raise Unsupported("unapproved static image URL")
    return url


class Downloads:
    def __init__(
        self,
        data_root,
        *,
        transport=None,
        image_origins=(),
        bulk_mirrors=(),
        plugin=None,
    ):
        self.root = Path(data_root).resolve()
        self.transport = transport

        self.origins = frozenset(_origin(base) for base in image_origins)
        self.mirror = Mirror(
            self, (_origin(base, mirror=True) for base in bulk_mirrors), plugin
        )

    def directory(self, path):
        target = Path(path)
        if (
            not target.is_absolute()
            or target.resolve() != target
            or not target.is_relative_to(self.root)
        ):
            raise Unsupported("static file outside profile")
        return target

    def _file(self, path, name, *, allow_json=False):
        target = self.directory(path) / segment(name)
        ext = target.suffix.lower()
        if ext not in EXTENSIONS and not (allow_json and ext == ".json"):
            raise Unsupported("invalid static file type")
        if target.resolve() != target or (
            target.exists() and target.stat().st_nlink != 1
        ):
            raise Unsupported("static file outside profile")
        return target

    async def _read(self, url, *, limit=MAX_BYTES):
        from httpx2 import AsyncClient, MockTransport
        from plugin_public_http import read_public_https

        if self.transport is None:
            code, _, body = await asyncio.to_thread(
                read_public_https, url, limit, accept="*/*", skip_error_body=True
            )
            return code, body
        if not isinstance(self.transport, MockTransport):
            raise Unsupported("only an in-memory alternate transport is supported")

        async with AsyncClient(
            transport=self.transport, timeout=8, follow_redirects=False, trust_env=False
        ) as client:
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    return response.status_code, b""
                body = bytearray()
                async for part in response.aiter_bytes(chunk_size=65536):
                    if len(body) + len(part) > limit:
                        raise Unsupported("static file exceeds size limit")
                    body.extend(part)
                return 200, bytes(body)

    @staticmethod
    def _image(data, size=None):
        image = Image.open(BytesIO(data))
        if image.format not in {"PNG", "JPEG", "WEBP"}:
            raise Unsupported("invalid static image format")
        _size(image.size)
        image.load()
        if size is not None:
            _size(size)
            return image.resize(size)
        return image

    @staticmethod
    def _write(target, data):
        # Preserve the upstream requirement for an existing destination directory.
        with NamedTemporaryFile(dir=target.parent, delete=False) as stream:
            temp = Path(stream.name)
            try:
                stream.write(data)
            except BaseException:
                stream.close()
                temp.unlink(missing_ok=True)
                raise
        try:
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)

    async def download(self, url, path, name, sess=None, tag=""):
        from httpx2 import RequestError

        target = self._file(path, name)
        _public_url(url, self.origins)
        if sess is not None:
            raise Unsupported("caller-supplied HTTP client is unavailable")
        try:
            code, data = await self._read(url)
            if code == 200:
                await asyncio.to_thread(self._image, data)
                await asyncio.to_thread(self._write, target, data)
            return code
        except (ssl.SSLError, PermissionError):
            raise
        except (RequestError, OSError):
            return None

    async def get_image(self, url, path, size=None, name=None):
        from httpx2 import ConnectError

        if size is not None:
            _size(size)
        target = self._file(path, name if name is not None else url.split("/")[-1])
        if target.is_file():
            try:
                with target.open("rb") as stream:
                    data = stream.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise Unsupported("static file exceeds size limit")
                return self._image(data, size)
            except UnidentifiedImageError:
                pass
        try:
            status, data = await self._read(_public_url(url, self.origins))
        except (PermissionError, ssl.SSLError):
            raise
        except (ConnectError, OSError):
            status, data = 0, b""
        if status != 200:
            return Image.new("RGBA", (256, 256))
        image = await asyncio.to_thread(self._image, data, size)
        await asyncio.to_thread(self._write, target, data)
        return image


def install_downloads(package, data_root, *, transport=None):
    profile = sys.modules.get("game_profile")
    port = Downloads(
        data_root,
        transport=transport,
        plugin=getattr(profile, "PACKAGE", None),
        image_origins=getattr(profile, "PUBLIC_IMAGE_ORIGINS", ()),
        bulk_mirrors=getattr(profile, "PUBLIC_BULK_MIRRORS", ()),
    )
    package("gsuid_core.utils.download_resource")
    package(
        "gsuid_core.utils.download_resource.download_core"
    ).download_all_file = port.mirror.download_all_file
    package("gsuid_core.utils.download_resource.download_file").download = port.download
    package(
        "gsuid_core.utils.download_resource.download_image"
    ).get_image = port.get_image
    return port
