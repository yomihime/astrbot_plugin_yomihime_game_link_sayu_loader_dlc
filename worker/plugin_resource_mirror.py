"""Read an authorized static Nginx mirror into a single profile."""

import asyncio
from collections import Counter, deque
from hashlib import sha256
from http.client import HTTPException, IncompleteRead
import ssl
from time import monotonic
from html.parser import HTMLParser
import json
from pathlib import PureWindowsPath
from urllib.parse import quote, unquote, urlsplit

from port_errors import OfflineNetwork, Unsupported

# HTTPS entries in the pinned native gsuid_core/utils/download_resource/download_core.py.
# The other native speed-test entries use plain HTTP.
DEFAULT_MIRRORS = (
    "https://sg-2.qxqx.cf",
    "https://kr.qxqx.cf",
    "https://jp.qxqx.cf",
    "https://gscore.focalors.com",
    "https://silverwing.elysia.beauty",
)


def segment(name):
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 195
        or name in (".", "..")
        or any(ord(c) < 32 or c in '<>:"/\\|?*' for c in name)
        or name[-1] in " ."
        or PureWindowsPath(name).is_reserved()
    ):
        raise Unsupported("invalid resource mirror path")
    return name


class _Index(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.inside = False
        self.done = False
        self.link = None
        self.rows = []
        self.listing = False
        self.entries = []

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        if tag == "pre":
            self.inside = self.listing = True
        if self.inside and tag == "a":
            self._finish()
            self.link = dict(attrs).get("href")

    def handle_endtag(self, tag):
        if self.inside and tag == "pre":
            self._finish()
            self.done = True
            self.inside = False

    def handle_data(self, data):
        if self.inside and self.link is not None:
            self.rows.append(data)

    def _finish(self):
        if self.link and self.link != "../":
            fields = "".join(self.rows).strip().split()
            if fields:
                self.entries.append((self.link, fields[-1]))
        self.link = None
        self.rows = []


class Mirror:
    def __init__(self, port, bases, owner):
        self.port = port
        self.bases = tuple(bases)
        self.owner = owner

        self.counts = Counter()
        self.failures = deque(maxlen=256)
        self.bytes_written = 0
        self.omitted_failures = 0
        self._selection_lock = asyncio.Lock()
        self._selected = None
        self._probed = False

    def diagnostics(self):
        return {
            "counts": dict(self.counts),
            "bytes_written": self.bytes_written,
            "failures": list(self.failures),
            "omitted_failures": self.omitted_failures,
        }

    def _failed(self, stage, url, reason, status=None):
        address = urlsplit(url)
        entry = {
            "stage": stage,
            "origin": address.hostname or "",
            "item_sha256": sha256(address.path.encode()).hexdigest(),
            "reason": reason,
        }
        if status is not None:
            entry["status"] = status
        self.counts[reason] += 1
        if len(self.failures) == self.failures.maxlen:
            self.omitted_failures += 1
        self.failures.append(entry)

    async def _resource(self, stage, url, limit):
        from httpx2 import RequestError

        try:
            code, data = await self.port._read(url, limit=limit)
        except OfflineNetwork:
            self._failed(stage, url, "offline")
            return None
        except (PermissionError, ssl.SSLError, Unsupported, IncompleteRead) as exc:
            self._failed(stage, url, type(exc).__name__)
            raise
        except (OSError, HTTPException, RequestError) as exc:
            cause = exc.__cause__
            while cause is not None:
                if isinstance(
                    cause, (PermissionError, ssl.SSLError, Unsupported, IncompleteRead)
                ):
                    self._failed(stage, url, type(cause).__name__)
                    raise cause
                cause = cause.__cause__
            self._failed(stage, url, "unreachable")
            return None
        if code in (401, 403, 407):
            self._failed(stage, url, "http_denied", code)
            raise PermissionError("static resource request was denied")
        if code != 200:
            self._failed(stage, url, "http_error", code)
            return None
        return data

    async def _probe(self, base):
        start = monotonic()
        try:
            data = await asyncio.wait_for(
                self._resource("probe", base + "/", 256 * 1024), timeout=8
            )
        except TimeoutError:
            self._failed("probe", base + "/", "unreachable")
            return None
        if data is None:
            return None
        if b"Index of /" not in data:
            self._failed("probe", base + "/", "no_index")
            return None
        return monotonic() - start, base

    async def _default(self):
        async with self._selection_lock:
            if not self._probed:
                rows = await asyncio.gather(
                    *(self._probe(b) for b in DEFAULT_MIRRORS), return_exceptions=True
                )
                # A mirror failing authorization or integrity cannot be selected;
                # its denial must propagate independently of other candidates.
                for row in rows:
                    if isinstance(row, BaseException):
                        raise row
                usable = [r for r in rows if r is not None]
                self._selected = min(usable)[1] if usable else None
                self._probed = True
            return self._selected

    async def download_all_file(self, plugin_name, EPATH_MAP, URL=None, TAG=None):
        if plugin_name != self.owner:
            self._failed("authorization", "", "namespace_denied")
            raise Unsupported("mirror namespace outside profile")
        base = (URL or "").rstrip("/")
        if URL and base not in (*DEFAULT_MIRRORS, *self.bases):
            self._failed("authorization", "", "origin_denied")
            raise Unsupported("unapproved resource mirror")
        endpoints = []
        for endpoint, folder in EPATH_MAP.items():
            try:
                path = self.port.directory(folder)
                if (
                    not isinstance(endpoint, str)
                    or not endpoint
                    or endpoint.startswith("/")
                ):
                    raise Unsupported("invalid mirror endpoint")
                route = "/".join(
                    quote(segment(p)) for p in endpoint.rstrip("/").split("/")
                )
                path.mkdir(parents=True, exist_ok=True)
            except (PermissionError, Unsupported, OSError) as exc:
                label = quote(endpoint, safe="") if isinstance(endpoint, str) else ""
                self._failed("directory", "/" + label, type(exc).__name__)
                raise
            endpoints.append((route, path))
        if not URL:
            base = await self._default()
        prefix = "/" + quote(segment(plugin_name)) + "/"
        budget = {"entries": 0, "bytes": 0, "pages": 0}
        try:
            async with asyncio.timeout(45):
                for route, path in endpoints:
                    url = (base or "") + prefix + route + "/"
                    if base:
                        await self._sync(url, path, budget, 0)
                    else:
                        self._failed("index", url, "no_available_mirror")
        except TimeoutError as exc:
            self._failed("batch", prefix, "deadline_exceeded")
            raise Unsupported(
                "resource synchronization exceeded its time limit"
            ) from exc

    async def _sync(self, url, folder, budget, depth):
        budget["pages"] += 1
        if budget["pages"] > 32 or depth > 3:
            raise Unsupported("resource directory limit exceeded")
        listing = await self._resource("index", url, 256 * 1024)
        if listing is None:
            return
        page = _Index()
        try:
            page.feed(listing.decode("utf-8"))
        except UnicodeError:
            self._failed("index", url, "invalid_index")
            raise
        page.close()
        if not page.listing:
            self._failed("index", url, "no_index")
            return
        if not page.done:
            self._failed("index", url, "incomplete_index")
            raise ValueError("incomplete static resource index")
        folder.mkdir(parents=True, exist_ok=True)
        for href, length in page.entries:
            try:
                await self._entry(url, folder, budget, depth, href, length)
            except (
                PermissionError,
                ssl.SSLError,
                Unsupported,
                ValueError,
                OSError,
            ) as exc:
                self._failed("entry", url + quote(href, safe=""), type(exc).__name__)
                raise

    async def _entry(self, url, folder, budget, depth, href, length):
        directory = href.endswith("/")
        # Resolve only a plain immediate child. Never request href itself.
        name = segment(unquote(href[:-1] if directory else href))
        child = url + quote(name)
        budget["entries"] += 1
        if budget["entries"] > 1024:
            raise Unsupported("resource entry limit exceeded")
        if directory and length == "-":
            await self._sync(
                child + "/", self.port.directory(folder / name), budget, depth + 1
            )
            return
        if directory or not length.isascii() or not length.isdecimal():
            return
        size = int(length)
        target = self.port._file(folder, name, allow_json=True)
        if target.is_file() and size and target.stat().st_size == size:
            self.counts["unchanged"] += 1
            return
        budget["bytes"] += size
        if size > 8 * 1024 * 1024 or budget["bytes"] > 32 * 1024 * 1024:
            raise Unsupported("resource mirror byte limit exceeded")
        data = await self._resource("file", child, size)
        if data is None:
            return
        if len(data) != size:
            self._failed("file", child, "size_mismatch")
            raise ValueError("incomplete static resource")
        if target.suffix.lower() == ".json":
            json.loads(data)
        else:
            await asyncio.to_thread(self.port._image, data)
        await asyncio.to_thread(self.port._write, target, data)
        self.bytes_written += len(data)
        self.counts["downloaded"] += 1
