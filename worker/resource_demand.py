"""Resolve actual reads beneath this generation's declared static mirror folders."""

from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
from hashlib import sha256
import os
import re
from pathlib import Path
import ssl
import sys
from threading import Event, Lock
from time import monotonic
from urllib.parse import quote, unquote

from plugin_resource_mirror import DEFAULT_MIRRORS, _Index, segment
from port_errors import Unsupported


_scope = ContextVar("private_static_read", default=None)
_installed = False
_extensions = {".json", ".png", ".jpg", ".jpeg", ".webp"}
_digest = re.compile(r"[0-9a-f]{64}")
_manifest_file_cap = 64
_manifest_bytes_cap = 8 * 1024 * 1024
_manifest_entry_cap = 65_536


class ResourcePath(type(Path())):
    """Preserve ordinary Path operations and resolve a missing public asset on a read probe."""

    __slots__ = ()

    def exists(self, *args, **kwargs):
        return super().exists(*args, **kwargs) or _missing(self)

    def is_file(self, *args, **kwargs):
        return super().is_file(*args, **kwargs) or _missing(self)


def _missing(path):
    lease = _scope.get()
    return lease is not None and lease.owner.ensure(lease, path)


def install_read_audit():
    global _installed
    if _installed:
        return
    read_flags = os.O_RDONLY
    for name in (
        "O_BINARY",
        "O_TEXT",
        "O_CLOEXEC",
        "O_NOINHERIT",
        "O_LARGEFILE",
        "O_NOFOLLOW",
        "O_NONBLOCK",
    ):
        read_flags |= getattr(os, name, 0)

    def check(event, args):
        if event != "open" or _scope.get() is None:
            return
        path, _, flags = args
        if isinstance(path, str) and not flags & ~read_flags:
            _missing(path)

    sys.addaudithook(check)
    _installed = True


@dataclass
class _Lease:
    owner: object
    end: float = 0
    active: bool = True
    requests: int = 0
    bytes: int = 0
    missed: set = field(default_factory=set)


class ResourceReads:
    def __init__(self, downloads, profile, reader):
        self.port = downloads
        self.root = downloads.root / "resources" / profile.PACKAGE
        self.namespace = "/" + quote(segment(profile.PACKAGE)) + "/"
        self.routes = frozenset(profile.STATIC_RESOURCE_DIRECTORIES)
        self.hash_prefixes = tuple(route + "/" for route in self.routes)
        self.hash_route = getattr(profile, "SHA256_MANIFEST_DIRECTORY", None)
        self.reader = reader
        self.folders = {}
        self.indexes = OrderedDict()
        self.checksums = OrderedDict()
        self.counts = Counter()
        self.closed = False
        self.serial = Lock()
        self.publication = Lock()
        self.metadata_lock = Lock()
        self.metadata_pool = None
        self.metadata_jobs = {}
        self.reported_failures = set()
        self.warmed = set()
        self.ready = False

    def declare(self, endpoints, base):
        for route, folder in endpoints:
            if (
                unquote(route) not in self.routes
                or folder == self.root
                or not folder.is_relative_to(self.root)
            ):
                continue
            prior = self.folders.get(folder)
            if prior and prior[0] != route:
                self.folders[folder] = ("", "")  # Ambiguous mappings fail closed.
            elif prior or len(self.folders) < 128:
                self.folders[folder] = (route, base)
        if self.ready:
            self.warm_declared()

    def start(self):
        self.ready = True
        self.warm_declared()

    def warm_declared(self):
        if not self.ready or not self.hash_route:
            return
        for route, explicit in self.folders.values():
            if route != self.hash_route:
                continue
            origin = explicit or self.port.mirror._selected
            if not origin:
                continue
            directory = origin + self.namespace + route + "/"
            with self.metadata_lock:
                if self.closed or directory in self.warmed or len(self.warmed) >= 2:
                    continue
                self.warmed.add(directory)
                self._metadata_job(directory)

    @contextmanager
    def scope(self):
        lease = _Lease(self)
        token = _scope.set(lease)
        try:
            yield
        finally:
            with self.publication:
                lease.active = False
            _scope.reset(token)

    def close(self):
        with self.publication:
            self.closed = True
        with self.metadata_lock:
            if self.metadata_pool is not None:
                self.metadata_pool.shutdown(wait=False, cancel_futures=True)

    def join(self):
        # Called after the broker's input closes and releases any TLS waiters.
        if self.metadata_pool is not None:
            self.metadata_pool.shutdown(wait=True, cancel_futures=True)

    def diagnostics(self):
        with self.metadata_lock:
            loads = []
            for directory, future in self.metadata_jobs.items():
                if future.cancelled() or self.closed:
                    state = "retired"
                elif not future.done():
                    state = "loading"
                else:
                    failure = future.exception()
                    state = (
                        type(failure).__name__ if failure is not None else "verified"
                    )
                loads.append({"origin": directory.split("/")[2], "state": state})
            return {
                "declared_folders": len(self.folders),
                "counts": dict(self.counts),
                "checksum_sets": loads,
            }

    def ensure(self, lease, value):
        # Strip the derived Path type before local checks to avoid recursive probes.
        target = Path(value)
        if (
            self.closed
            or not lease.active
            or not target.is_relative_to(self.root)
            or target.suffix.lower() not in _extensions
            or target.is_file()
        ):
            return False
        parent = next((p for p in target.parents if p in self.folders), None)
        if parent is None or not self.folders[parent][0]:
            self.counts["undeclared"] += 1
            return False
        parts = target.relative_to(parent).parts
        if len(parts) > 4 or target.resolve() != target:
            self.counts["invalid_path"] += 1
            raise Unsupported("invalid static read path")
        if target in lease.missed:
            return False
        if not lease.end:
            lease.end = monotonic() + 12
        if not self.serial.acquire(timeout=max(0.0, lease.end - monotonic())):
            self.counts["read_budget"] += 1
            raise TimeoutError("static read queue deadline exceeded")
        try:
            if target.is_file():
                return True
            if self.closed or not lease.active or target in lease.missed:
                return False
            if monotonic() >= lease.end:
                self.counts["read_budget"] += 1
                raise TimeoutError("static read budget exhausted")
            if lease.requests >= 16:
                self.counts["read_item_limit"] += 1
                raise Unsupported("static read item limit reached")
            lease.requests += 1
            token = _scope.set(None)
            try:
                result = self._resolve(parent, parts, target, lease)
            except (PermissionError, ssl.SSLError, Unsupported, ValueError) as exc:
                self.counts[type(exc).__name__] += 1
                raise
            except OSError as exc:
                self.counts[type(exc).__name__] += 1
                raise
            finally:
                _scope.reset(token)
            if not result:
                lease.missed.add(target)
            return result
        finally:
            self.serial.release()

    def _read(self, url, limit, deadline):
        if monotonic() >= deadline:
            raise TimeoutError("static read deadline exceeded")
        status, _, data = self.reader.read_before(url, limit, deadline)
        if status in (401, 403, 407):
            raise PermissionError("public mirror denied static read")
        if status != 200:
            self.counts[f"http_{status}"] += 1
            if status in (404, 410):
                return None
            raise OSError(f"static resource returned HTTP {status}")
        if len(data) > limit:
            raise Unsupported("static read exceeds its byte limit")
        return data

    def _index(self, url, deadline, *, fresh=False):
        if fresh:
            return self._listing(url, deadline)
        cached = self.indexes.get(url)
        if not fresh and cached and monotonic() < cached[0]:
            self.indexes.move_to_end(url)
            return cached[1]
        entries = self._listing(url, deadline)
        if entries is not None:
            self.indexes[url] = (monotonic() + 60, entries)
            if len(self.indexes) > 32:
                self.indexes.popitem(last=False)
        return entries

    def _listing(self, url, deadline):
        data = self._read(url, 256 * 1024, deadline)
        if data is None:
            return None
        page = _Index()
        page.feed(data.decode("utf-8"))
        page.close()
        if not page.listing or not page.done or len(page.entries) > 1024:
            raise Unsupported("invalid static directory index")
        return page.entries

    def _source(self, parent, lease):
        route, base = self.folders[parent]
        base = base or self.port.mirror._selected
        unavailable = None
        for origin in (base,) if base else DEFAULT_MIRRORS:
            url = origin + self.namespace + route + "/"
            try:
                entries = self._index(
                    url, lease.end if base else min(lease.end, monotonic() + 1.5)
                )
            except (PermissionError, ssl.SSLError):
                raise
            except OSError as exc:
                if base:
                    raise
                unavailable = exc
                self.counts["mirror_unavailable"] += 1
                continue
            if entries is not None:
                return url, entries
        if unavailable is not None:
            raise unavailable
        return None, None

    def _resolve(self, parent, parts, target, lease):
        for part in parts:
            segment(part)
        url, entries = self._source(parent, lease)
        root_url = url
        for index, part in enumerate(parts):
            if entries is None:
                return False
            directory = index < len(parts) - 1
            matches = [
                (h, n)
                for h, n in entries
                if h.endswith("/") == directory
                and unquote(h.rstrip("/"), errors="strict") == part
            ]
            if not matches:
                self.counts["unlisted"] += 1
                return False
            if len(matches) != 1:
                raise Unsupported("ambiguous static index entry")
            href, size = matches[0]
            # Only the publisher's verified immediate-child name becomes a request.
            url += quote(segment(unquote(href.removesuffix("/"), errors="strict")))
            if directory:
                if size != "-":
                    raise Unsupported("invalid static directory entry")
                url += "/"
                entries = self._index(url, lease.end)
        if not size.isascii() or not size.isdecimal() or len(size) > 8:
            raise Unsupported("invalid published file size")
        expected = int(size)
        if expected > 8 * 1024 * 1024 or lease.bytes + expected > 24 * 1024 * 1024:
            raise Unsupported("static read byte budget exceeded")
        lease.bytes += expected
        data = self._read(url, expected, lease.end)
        if data is None:
            return False
        if len(data) != expected:
            raise Unsupported("incomplete static file")
        if self.hash_route:
            route = self.folders[parent][0]
            origin = root_url.removesuffix(self.namespace + route + "/")
            source_key = unquote(url.removeprefix(origin + self.namespace))
            self._verify_hash(origin, source_key, data, lease)
        if target.suffix.lower() == ".json":
            json.loads(data)
        else:
            self.port._image(data).close()
        with self.publication:
            if self.closed or not lease.active:
                self.counts["retired"] += 1
                return False
            self.port._file(target.parent, target.name, allow_json=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            created = self.port._write(target, data, replace=False)
            self.counts["published" if created else "existing"] += 1
            return target.is_file()

    def _verify_hash(self, origin, source_key, data, lease):
        # XutheringWavesUID's check_resource_hash compares SHA-256 values from
        # each published JSON object against repository-relative source paths.
        if not any(
            route == self.hash_route and (base or self.port.mirror._selected) == origin
            for route, base in self.folders.values()
        ):
            raise Unsupported(
                "upstream checksum directory was not declared for this mirror"
            )
        directory = origin + self.namespace + self.hash_route + "/"
        actual = sha256(data).digest()
        with self.metadata_lock:
            stamp = self.checksums.get(directory)
            if stamp and monotonic() < stamp[0] and stamp[1].get(source_key) == actual:
                self.checksums.move_to_end(directory)
                self.counts["checksum_set_cache_hits"] += 1
                return
            future = self._metadata_job(directory, 24 * 1024 * 1024 - lease.bytes)
        # A published asset never becomes 'absent' because the full checksum
        # collection is still in flight. A caller may retry within a new lease.
        try:
            digests, size = future.result(timeout=max(0, lease.end - monotonic()))
        except Exception as exc:
            with self.metadata_lock:
                if self.metadata_jobs.get(directory) is future and future.done():
                    self.reported_failures.add(directory)
                elif isinstance(exc, TimeoutError):
                    self.counts["checksum_wait_timeout"] += 1
                    raise TimeoutError(
                        "complete public checksum collection is still loading"
                    ) from exc
            raise
        if lease.bytes + size > 24 * 1024 * 1024:
            raise Unsupported("published checksum collection exceeds read budget")
        lease.bytes += size
        expected = digests.get(source_key)
        if expected is not None and expected != actual:
            self.counts["unverified_checksum"] += 1
            raise Unsupported("incorrect published checksum")

    def _metadata_job(self, directory, limit=_manifest_bytes_cap):
        # Caller holds metadata_lock. The source identity is the full declared
        # directory including the selected origin. Files from mirrors never mix.
        if self.closed:
            raise Unsupported("public resource generation retired")
        prior = self.metadata_jobs.get(directory)
        if prior is not None:
            if not prior.done():
                self.counts["checksum_set_joined"] += 1
                return prior
            if (
                prior.exception() is not None
                and directory not in self.reported_failures
            ):
                return prior  # Surface the recorded failure before a same-source retry.
        elif len(self.metadata_jobs) >= 2:
            retired = next((d for d, f in self.metadata_jobs.items() if f.done()), None)
            if retired is None:
                raise Unsupported("concurrent checksum origin limit exceeded")
            del self.metadata_jobs[retired]
            self.reported_failures.discard(retired)
        self.reported_failures.discard(directory)
        if self.metadata_pool is None:
            self.metadata_pool = ThreadPoolExecutor(max_workers=1)
        future = self.metadata_pool.submit(
            self._prepare_set, directory, min(limit, _manifest_bytes_cap)
        )
        self.metadata_jobs[directory] = future
        return future

    def _prepare_set(self, directory, limit):
        # This is part of the original requested bulk metadata, with an
        # independent bounded deadline. A single slow chat must not cancel it.
        initial = 24 * 1024 * 1024 - limit
        lease = _Lease(self, end=monotonic() + 24, bytes=initial)
        try:
            digests = self._load_checksums(directory, lease)
            with self.metadata_lock:
                if self.closed:
                    raise Unsupported("public resource generation retired")
                self.checksums[directory] = (monotonic() + 60, digests)
                if len(self.checksums) > 2:
                    self.checksums.popitem(last=False)
                self.counts["checksum_sets_loaded"] += 1
            return digests, lease.bytes - initial
        except Exception as exc:
            with self.metadata_lock:
                self.checksums.pop(directory, None)
                self.counts["checksum_set_failure_" + type(exc).__name__] += 1
            raise

    def _load_checksums(self, directory, lease):
        listing = self._index(directory, lease.end, fresh=True)
        if listing is None:
            raise Unsupported("published checksum directory unavailable")
        files = {}
        for href, length in listing:
            if not href.lower().endswith(".json"):
                continue
            name = segment(unquote(href, errors="strict"))
            if not length.isascii() or not length.isdecimal() or len(length) > 8:
                raise Unsupported("invalid published checksum file size")
            size = int(length)
            if name in files or size > 4 * 1024 * 1024:
                raise Unsupported("duplicate or oversized published checksum file")
            files[name] = size
        total = sum(files.values())
        if (
            len(files) > _manifest_file_cap
            or total > _manifest_bytes_cap
            or lease.bytes + total > 24 * 1024 * 1024
        ):
            raise Unsupported("published checksum collection exceeds budget")
        lease.bytes += total

        cancelled = Event()

        def load(item):
            if cancelled.is_set() or self.closed or not lease.active:
                raise Unsupported("static read retired")
            name, size = item
            raw = self._read(directory + quote(name), size, lease.end)
            if raw is None or len(raw) != size:
                raise Unsupported("incomplete published checksum data")
            return raw

        # Leave independent slots for foreground data and the original bulk job.
        digests = {}
        entries = 0
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = [pool.submit(load, item) for item in files.items()]
            try:
                for result in results:
                    # Preserve duplicate members for conflict detection; arrays
                    # and nested objects cannot stand in for a SHA-256 value.
                    pairs = json.loads(result.result(), object_pairs_hook=tuple)
                    if not isinstance(pairs, tuple):
                        raise Unsupported("invalid checksum manifest")
                    entries += len(pairs)
                    if entries > _manifest_entry_cap:
                        raise Unsupported("published checksum entry limit exceeded")
                    for key, value in pairs:
                        # Native resolve_local_path ignores metadata and paths
                        # outside registered roots before looking at the digest.
                        if not key.startswith(self.hash_prefixes) or ".." in key.split(
                            "/"
                        ):
                            continue
                        if (
                            len(key) > 1024
                            or not isinstance(value, str)
                            or not _digest.fullmatch(value)
                        ):
                            raise Unsupported("invalid published checksum record")
                        digest = bytes.fromhex(value)
                        if digests.setdefault(key, digest) != digest:
                            self.counts["unverified_checksum"] += 1
                            raise Unsupported("conflicting published checksum")
            finally:
                cancelled.set()
                for result in results:
                    result.cancel()
        if self.closed or not lease.active or monotonic() >= lease.end:
            raise TimeoutError("checksum set validation did not complete in time")
        return digests
