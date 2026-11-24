"""Read an authorized static Nginx mirror into a single profile."""

import asyncio
from collections import Counter, deque
from dataclasses import dataclass, field
from hashlib import sha256
from http.client import HTTPException, IncompleteRead
import ssl
from time import monotonic
from html.parser import HTMLParser
import json
from pathlib import PureWindowsPath
from urllib.parse import quote, unquote, urlsplit

from port_errors import OfflineNetwork, StartupTransferLimit, Unsupported

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


@dataclass
class _Batch:
    roots: deque
    prefix: str
    base: str
    pending: list = field(default_factory=list)
    entries: int = 0
    pages: int = 0
    bytes: int = 0
    failed_reads: int = 0
    published_files: int = 0
    published_bytes: int = 0
    state: str = "queued"

    def inventory(self):
        return {
            "state": self.state,
            "pending_roots": len(self.roots),
            "failed_reads": self.failed_reads,
            "published_files": self.published_files,
            "published_bytes": self.published_bytes,
        }


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
        self._continuations = {}
        self._runner = None
        self.demand = None

    def diagnostics(self):
        return {
            "counts": dict(self.counts),
            "bytes_written": self.bytes_written,
            "failures": list(self.failures),
            "omitted_failures": self.omitted_failures,
            "continuations": [b.inventory() for b in self._continuations.values()],
            "on_demand": self.demand.diagnostics() if self.demand is not None else {},
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
                    if isinstance(row, BaseException) and not isinstance(
                        row, StartupTransferLimit
                    ):
                        raise row
                for row in rows:
                    if isinstance(row, StartupTransferLimit):
                        raise row
                usable = [r for r in rows if r is not None]
                self._selected = min(usable)[1] if usable else None
                from resource_broker import CLIENT

                # A short startup probe is not conclusive for later user-initiated
                # refreshes, which have the normal public transport budget.
                self._probed = (
                    bool(self._selected) or CLIENT is None or CLIENT.budget is None
                )
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
        if len(EPATH_MAP) > 64:
            raise Unsupported("resource root limit exceeded")
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
        from resource_broker import CLIENT, transfer_window

        prefix = "/" + quote(segment(plugin_name)) + "/"
        if self.demand is not None:
            self.demand.declare(endpoints, base)
        key = (base, frozenset(endpoints))
        if (
            CLIENT is not None
            and CLIENT.budget is not None
            and key in self._continuations
        ):
            return None
        batch = _Batch(deque(endpoints), prefix, base)
        try:
            with transfer_window() as deadline:
                async with asyncio.timeout(45):
                    await self._run(batch, deadline)
        except StartupTransferLimit:
            self._failed("batch", prefix, "startup_budget_exhausted")
            for route, _ in batch.roots:
                self._failed(
                    "index", (base or "") + prefix + route + "/", "startup_incomplete"
                )
            if CLIENT is not None and CLIENT.budget is not None and batch.roots:
                if len(self._continuations) < 4:
                    self._continuations.setdefault(key, batch)
                else:
                    self._failed("batch", prefix, "continuation_capacity")
            return None
        except TimeoutError as exc:
            self._failed("batch", prefix, "deadline_exceeded")
            raise Unsupported(
                "resource synchronization exceeded its time limit"
            ) from exc

    def start_continuations(self):
        if self._runner is None and self._continuations:
            self._runner = asyncio.create_task(self._resume())

    async def stop_continuations(self):
        if self._runner is not None:
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)
        for batch in self._continuations.values():
            if batch.state in ("queued", "running"):
                batch.state = "cancelled"

    async def _resume(self):
        # One runner per generation, at most four original 32 MiB batches.
        # The wall limit bounds the entire queue, including mirror selection.
        end = monotonic() + 60
        for batch in self._continuations.values():
            if monotonic() >= end:
                self._failed("continuation", batch.prefix, "deadline_exceeded")
                batch.state = "partial"
                continue
            batch.state = "running"
            try:
                async with asyncio.timeout(min(45, end - monotonic())):
                    await self._run(batch, None)
            except asyncio.CancelledError:
                batch.state = "cancelled"
                raise
            except TimeoutError:
                self._failed("continuation", batch.prefix, "deadline_exceeded")
                batch.state = "partial"
            except Exception as exc:
                self._failed("continuation", batch.prefix, type(exc).__name__)
                batch.state = "failed"
            else:
                batch.state = "partial" if batch.failed_reads else "completed"

    async def _run(self, batch, deadline):
        if not batch.base:
            batch.base = await self._default()
        while batch.roots:
            route, path = batch.roots[0]
            url = (batch.base or "") + batch.prefix + route + "/"
            if not batch.base:
                self._failed("index", url, "no_available_mirror")
                batch.failed_reads += 1
            else:
                if not batch.pending:
                    batch.pending.append((url, path, 0, None, None))
                # Keep the interrupted item and its unvisited siblings. Neither
                # completed files nor already-observed failures are re-requested.
                while batch.pending:
                    if deadline is not None and monotonic() >= deadline:
                        raise StartupTransferLimit("startup transfer budget exhausted")
                    parent, folder, depth, href, length = batch.pending[-1]
                    folder = self.port.directory(folder)
                    if href is None:
                        children = await self._index(parent, folder, batch, depth)
                        batch.pages += 1
                    else:
                        try:
                            children = await self._entry(
                                parent, folder, batch, depth, href, length
                            )
                        except (
                            PermissionError,
                            ssl.SSLError,
                            Unsupported,
                            ValueError,
                            OSError,
                        ) as exc:
                            self._failed(
                                "entry",
                                parent + quote(href, safe=""),
                                type(exc).__name__,
                            )
                            raise
                        batch.entries += 1
                    batch.pending.pop()
                    if children:
                        batch.pending.extend(reversed(children))
            batch.roots.popleft()

    async def _index(self, url, folder, batch, depth):
        if batch.pages >= 32 or depth > 3:
            raise Unsupported("resource directory limit exceeded")
        listing = await self._resource("index", url, 256 * 1024)
        if listing is None:
            batch.failed_reads += 1
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
            batch.failed_reads += 1
            return
        if not page.done:
            self._failed("index", url, "incomplete_index")
            raise ValueError("incomplete static resource index")
        folder.mkdir(parents=True, exist_ok=True)
        if len(page.entries) > 1024:
            raise Unsupported("resource entry limit exceeded")
        return [(url, folder, depth, href, length) for href, length in page.entries]

    async def _entry(self, url, folder, batch, depth, href, length):
        directory = href.endswith("/")
        # Resolve only a plain immediate child. Never request href itself.
        name = segment(unquote(href[:-1] if directory else href))
        child = url + quote(name)
        if batch.entries >= 1024:
            raise Unsupported("resource entry limit exceeded")
        if directory and length == "-":
            return [
                (child + "/", self.port.directory(folder / name), depth + 1, None, None)
            ]
        if directory or not length.isascii() or not length.isdecimal():
            return
        size = int(length)
        target = self.port._file(folder, name, allow_json=True)
        existing = target.is_file()
        if existing and size and target.stat().st_size == size:
            self.counts["unchanged"] += 1
            return
        if size > 8 * 1024 * 1024 or batch.bytes + size > 32 * 1024 * 1024:
            raise Unsupported("resource mirror byte limit exceeded")
        data = await self._resource("file", child, size)
        batch.bytes += size
        if data is None:
            batch.failed_reads += 1
            return
        if len(data) != size:
            self._failed("file", child, "size_mismatch")
            raise ValueError("incomplete static resource")
        if target.suffix.lower() == ".json":
            json.loads(data)
        else:
            await asyncio.to_thread(self.port._image, data)
        # An atomic file publication already in progress must settle before
        # retirement can release the private generation's storage lock.
        write = asyncio.create_task(self._publish(target, data, batch, replace=existing))
        try:
            await asyncio.shield(write)
        except asyncio.CancelledError:
            await write
            raise

    async def _publish(self, target, data, batch, *, replace):
        if not await asyncio.to_thread(self.port._write, target, data, replace=replace):
            self.counts["unchanged"] += 1
            return
        self.bytes_written += len(data)
        self.counts["downloaded"] += 1
        batch.published_files += 1
        batch.published_bytes += len(data)
