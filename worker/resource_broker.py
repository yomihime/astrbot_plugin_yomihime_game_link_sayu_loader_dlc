"""Private pipe to a resource reader which never imports game plugin code."""

from concurrent.futures import Future
from contextlib import contextmanager, nullcontext
from http.client import IncompleteRead
from io import BytesIO
import asyncio
import itertools
import json
import os
from pathlib import PurePosixPath
import re
import ssl
import subprocess
import sys
from threading import BoundedSemaphore, Lock, Thread
from time import monotonic
from urllib.parse import unquote, urlsplit

from port_errors import StartupTransferLimit, Unsupported

MAX_IMAGE = 16 * 1024 * 1024
MAX_FILE = 8 * 1024 * 1024
MAX_INDEX = 256 * 1024
IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}
CLIENT = None
# A host-package restore may start three generations inside one 30 s lifecycle
# window. Real cold staging/imports consume the rest; this is shared by all
# startup transfers in a generation, including concurrent mirror probes.
STARTUP_SECONDS = 3.0


class StartupBudget:
    def __init__(self, seconds):
        self.seconds = self.remaining = seconds
        self.active = 0
        self.exhausted = False
        self.started = None
        self.lock = Lock()

    @contextmanager
    def transfer(self):
        with self.lock:
            now = monotonic()
            if self.active == 0:
                self.started = now
            deadline = self.started + self.remaining
            if deadline <= now:
                self.exhausted = True
                raise StartupTransferLimit("startup transfer budget exhausted")
            self.active += 1
        try:
            yield deadline
        except StartupTransferLimit:
            with self.lock:
                self.exhausted = True
            raise
        finally:
            with self.lock:
                self.active -= 1
                if self.active == 0:
                    self.remaining = max(0.0, deadline - monotonic())

    def diagnostics(self):
        return {
            "limit_seconds": self.seconds,
            "spent_seconds": round(self.seconds - self.remaining, 3),
            "exhausted": self.exhausted,
        }


@contextmanager
def startup_resources():
    from resource_httpx import stream_scope

    with stream_scope(), _startup_budget():
        yield


@contextmanager
def _startup_budget():
    reader = CLIENT
    if reader is None:
        yield
        return
    reader.budget = StartupBudget(STARTUP_SECONDS)
    try:
        yield
    finally:
        reader.startup_diagnostics = reader.budget.diagnostics()
        reader.budget = None


def transfer_window():
    budget = CLIENT.budget if CLIENT is not None else None
    return budget.transfer() if budget is not None else nullcontext(None)


def ambr_asset(url):
    # gsuid_core/utils/api/ambr/{api,request}.py (96cd7353). get_ambr_icon
    # constructs one PNG filename, optionally under one asset-type directory.
    if url == "https://gi.yatta.moe/assets/data/event.json":
        return "json"
    if re.fullmatch(
        r"https://gi\.yatta\.moe/assets/UI/(?:UI_[A-Za-z0-9_-]+|[A-Za-z0-9_-]+/[A-Za-z0-9_-]+)\.png",
        url,
    ):
        return "image"
    return None


def allowed(url, profile):
    from plugin_resource_mirror import DEFAULT_MIRRORS, segment

    if (
        not isinstance(url, str)
        or len(url) > 2048
        or not url.isascii()
        or any(ord(c) < 33 or ord(c) == 127 for c in url)
        or "?" in url
        or "#" in url
    ):
        raise Unsupported("invalid static resource URL")
    target = urlsplit(url)
    if (
        target.scheme != "https"
        or not target.hostname
        or target.netloc != target.hostname
    ):
        raise Unsupported("invalid static resource origin")
    origin = "https://" + target.netloc
    path = target.path
    if (
        not path.startswith("/")
        or "//" in path
        or re.search("%(?![0-9a-f]{2})|%(?:2f|5c|25)", path, re.I)
    ):
        raise Unsupported("invalid static resource path")
    for component in path.strip("/").split("/") if path != "/" else ():
        segment(unquote(component, errors="strict"))
    suffix = PurePosixPath(path).suffix.lower()
    if profile.PACKAGE == "GenshinUID" and (kind := ambr_asset(url)):
        return kind, MAX_FILE
    if suffix in IMAGES:
        if url in profile.STATIC_RESOURCE_URLS:
            return "image", MAX_IMAGE
        if origin in profile.PUBLIC_IMAGE_ORIGINS:
            return "image", MAX_FILE
    for base in (*DEFAULT_MIRRORS, *profile.PUBLIC_BULK_MIRRORS):
        if url == base + "/":
            return "index", MAX_INDEX
        for directory in profile.STATIC_RESOURCE_DIRECTORIES:
            if url.startswith(base + "/" + profile.PACKAGE + "/" + directory + "/"):
                if path.endswith("/"):
                    return "index", MAX_INDEX
                if suffix in IMAGES:
                    return "image", MAX_FILE
                if suffix == ".json":
                    return "json", MAX_FILE
    raise Unsupported("resource is outside the pinned public declarations")


def fetch(url, limit, profile, deadline=None):
    from plugin_public_http import read_public_https

    kind, ceiling = allowed(url, profile)
    if type(limit) is not int or not 0 <= limit <= MAX_IMAGE:
        raise Unsupported("invalid resource byte budget")
    timing = {} if deadline is None else {"deadline": deadline}
    # A plain upstream client observes a genuine HTTP error response. Preserve
    # only complete, small text/JSON error bodies from its source-literal assets.
    source_literal = url in profile.STATIC_RESOURCE_URLS
    error_policy = {"error_body_limit": 8192} if source_literal else {}
    if getattr(profile, "SHA256_MANIFEST_DIRECTORY", None) and any(
        url.startswith(base + "/" + profile.PACKAGE + "/")
        for base in profile.PUBLIC_BULK_MIRRORS
    ):
        # A digest set spans multiple anonymous GETs to the same public mirror.
        # DNS and the established socket peer are checked on every reuse.
        timing["reuse_connection"] = True
    status, raw_headers, data = read_public_https(
        url,
        min(limit, ceiling),
        accept={"image": "image/*", "index": "text/html", "json": "application/json"}[
            kind
        ],
        skip_error_body=True,
        **error_policy,
        **timing,
    )
    if status != 200:
        if source_literal and 400 <= status < 500 and 0 < len(data) <= 8192:
            from plugin_public_http import ERROR_MEDIA_TYPES

            mime = next(
                (
                    v.partition(";")[0].strip().lower()
                    for k, v in raw_headers
                    if k.lower() == "content-type"
                ),
                "",
            )
            if mime in ERROR_MEDIA_TYPES:
                return status, [("Content-Type", mime)], data
        return status, [], b""
    if kind == "image":
        from PIL import Image

        try:
            with Image.open(
                BytesIO(data), formats=("PNG", "JPEG", "WEBP", "GIF", "AVIF")
            ) as image:
                if image.width * image.height > 12_000_000:
                    raise Unsupported("published image exceeds the source pixel budget")
                mime = Image.MIME[image.format]
                image.verify()
        except OSError as exc:
            raise Unsupported("invalid public image") from exc
    elif kind == "json":
        mime = "application/json"
        json.loads(data)
    else:
        mime = "text/html"
        if b"Index of /" not in data and b"<pre" not in data.lower():
            # A 200 with an unrelated page is not a directory listing.
            data = b""
    return status, [("Content-Type", mime)], data


def _header(value):
    return (
        json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        + b"\n"
    )


def stream_spec(url, profile):
    for declared, mime, size in getattr(profile, "STATIC_STREAM_OBJECTS", ()):
        if (
            url == declared
            and mime == "application/octet-stream"
            and type(size) is int
            and 0 < size <= 1048576
        ):
            return mime, size
    raise Unsupported("object is outside the static stream declarations")


def serve():
    import game_profile

    if not game_profile.PUBLIC_RESOURCES_ONLY:
        raise Unsupported("resource process not enabled")
    incoming, outgoing = sys.stdin.buffer, sys.stdout.buffer
    writer = Lock()
    slots = BoundedSemaphore(5)
    controls = {}
    controls_lock = Lock()

    def write(number, status, headers, data):
        with writer:
            outgoing.write(_header([number, status, headers, len(data)]))
            outgoing.write(data)
            outgoing.flush()

    def stream(number, url, deadline, control):
        from plugin_public_http import stream_public_https

        def emit(status, headers, data):
            if control.stopped:
                raise InterruptedError("stream retired")
            write(number, status, headers, data)

        try:
            mime, size = stream_spec(url, game_profile)
            if type(deadline) not in (float, int) or not 0 < deadline < float("inf"):
                raise Unsupported("invalid public stream deadline")
            stream_public_https(
                url, mime, size, min(deadline, monotonic() + 15), control, emit
            )
            emit("eof", [], b"")
        except (ConnectionError, InterruptedError, TimeoutError):
            write(number, "unavailable", [], b"")
        except ssl.SSLError:
            write(number, "tls", [], b"")
        except PermissionError:
            write(number, "denied", [], b"")
        except Exception:
            write(number, "invalid", [], b"")
        finally:
            with controls_lock:
                controls.pop(number, None)
            slots.release()

    def respond(number, url, limit, deadline):
        try:
            try:
                if deadline is not None and (
                    type(deadline) not in (float, int)
                    or not 0 < deadline < float("inf")
                ):
                    raise Unsupported("invalid transfer deadline")
                status, headers, data = fetch(url, limit, game_profile, deadline)
                reply = [number, status, headers, len(data)]
            except ssl.SSLError:
                reply, data = [number, "tls", [], 0], b""
            except PermissionError:
                reply, data = [number, "denied", [], 0], b""
            except TimeoutError:
                reason = (
                    "budget"
                    if deadline is not None and monotonic() >= deadline
                    else "unavailable"
                )
                reply, data = [number, reason, [], 0], b""
            except (Unsupported, ValueError, SyntaxError, IncompleteRead):
                reply, data = [number, "invalid", [], 0], b""
            except OSError:
                reply, data = [number, "unavailable", [], 0], b""
            except Exception:
                reply, data = [number, "invalid", [], 0], b""
            with writer:
                outgoing.write(_header(reply))
                outgoing.write(data)
                outgoing.flush()
        finally:
            slots.release()

    while line := incoming.readline(8193):
        if len(line) > 8192 or not line.endswith(b"\n"):
            break
        request = json.loads(line)
        if not (
            isinstance(request, list) and len(request) == 4 and type(request[0]) is int
        ):
            break
        number, arg, limit, deadline = request
        if arg in ("cancel_stream", "ack_stream"):
            with controls_lock:
                control = controls.get(number)
                if control is not None:
                    if arg == "cancel_stream":
                        control.cancel()
                    else:
                        control.acknowledge()
            continue
        if not slots.acquire(blocking=False):
            write(number, "unavailable", [], b"")
            continue
        if arg == "stream":
            from plugin_public_http import StreamControl

            control = StreamControl()
            with controls_lock:
                controls[number] = control
            Thread(
                target=stream, args=(number, limit, deadline, control), daemon=True
            ).start()
            continue
        # Each reader is bounded and stateless. Pipe EOF must also retire a
        # reader whose DNS resolver is still waiting after its owner exits.
        Thread(target=respond, args=request, daemon=True).start()


class Stream:
    """One bounded wire response, received on the reader thread, consumed on its event loop."""

    def __init__(self, reader, number, deadline):
        self.reader = reader
        self.number = number
        self.deadline = deadline
        self.loop = asyncio.get_running_loop()
        self.frames = asyncio.Queue()
        self.closed = False
        self.consumed = 0

    async def next(self):
        async with asyncio.timeout_at(
            self.loop.time() + max(0, self.deadline - monotonic())
        ):
            status, headers, data = await self.frames.get()
        if status == "tls":
            raise ssl.SSLError("public stream TLS verification failed")
        if status == "denied":
            raise PermissionError("public stream denied")
        if status == "invalid":
            raise Unsupported("invalid public stream response")
        if status == "unavailable":
            raise ConnectionError("public stream unavailable")
        if status == "chunk" and not self.closed:
            self.consumed += 1
            if self.consumed % 4 == 0:
                with self.reader.lock:
                    self.reader.process.stdin.write(
                        _header([self.number, "ack_stream", None, None])
                    )
                    self.reader.process.stdin.flush()
        return status, headers, data

    def cancel(self):
        if self.closed:
            return
        self.closed = True
        with self.reader.lock:
            self.reader.streams.pop(self.number, None)
            try:
                if not self.reader.process.stdin.closed:
                    self.reader.process.stdin.write(
                        _header([self.number, "cancel_stream", None, None])
                    )
                    self.reader.process.stdin.flush()
            except (OSError, ValueError):
                pass
        # Wake an iterator whose callback retired before the next network frame.
        self.frames.put_nowait(("unavailable", [], b""))
        self.reader.slots.release()


class Reader:
    def __init__(self, process):
        self.process = process
        self.lock = Lock()
        self.ids = itertools.count(1)
        self.waiting = {}
        self.streams = {}
        self.budget = None
        self.startup_diagnostics = {}
        self.slots = BoundedSemaphore(5)
        self.thread = Thread(target=self.receive, daemon=True)
        self.thread.start()

    def close(self):
        # Wake pending reads before asyncio drains its executor on retirement.
        with self.lock:
            self.process.stdin.close()

    def receive(self):
        stream = self.process.stdout
        try:
            while line := stream.readline(4097):
                if len(line) > 4096:
                    break
                number, status, headers, size = json.loads(line)
                if type(size) is not int or not 0 <= size <= MAX_IMAGE:
                    break
                data = stream.read(size)
                if len(data) != size:
                    break
                with self.lock:
                    pipe = self.streams.get(number)
                    future = self.waiting.pop(number, None)
                if pipe is not None:
                    pipe.loop.call_soon_threadsafe(
                        pipe.frames.put_nowait, (status, headers, data)
                    )
                elif future is not None:
                    future.set_result((status, headers, data))
        finally:
            with self.lock:
                for future in self.waiting.values():
                    future.set_exception(ConnectionError("resource reader closed"))
                self.waiting.clear()
                for pipe in self.streams.values():
                    pipe.loop.call_soon_threadsafe(
                        pipe.frames.put_nowait, ("unavailable", [], b"")
                    )

    def stream(self, url, deadline):
        if not self.slots.acquire(blocking=False):
            raise TimeoutError("public reader slots unavailable")
        with self.lock:
            number = next(self.ids)
            try:
                pipe = Stream(self, number, deadline)
                self.streams[number] = pipe
                self.process.stdin.write(_header([number, "stream", url, deadline]))
                self.process.stdin.flush()
                return pipe
            except BaseException:
                self.streams.pop(number, None)
                self.slots.release()
                raise

    def read(self, url, limit):
        if not isinstance(url, str) or len(url) > 2048 or not url.isascii():
            raise Unsupported("invalid public resource request")
        if type(limit) is not int or not 0 <= limit <= MAX_IMAGE:
            raise Unsupported("invalid resource byte budget")
        with transfer_window() as deadline:
            # Probe all default mirrors concurrently without spending the entire
            # cold-start window waiting for a single slow root directory.
            probe = False
            if deadline is not None:
                from plugin_resource_mirror import DEFAULT_MIRRORS

                probe = url.rstrip("/") in DEFAULT_MIRRORS
                if probe:
                    deadline = (monotonic() + deadline) / 2
            return self.request(url, limit, deadline, probe)

    def request(self, url, limit, deadline, probe):
        def remaining():
            return 18 if deadline is None else max(0, deadline - monotonic())

        if not self.slots.acquire(timeout=remaining()):
            if deadline is not None and not probe:
                raise StartupTransferLimit("startup transfer budget exhausted")
            raise TimeoutError("public reader slots unavailable")
        try:
            future = Future()
            with self.lock:
                if self.process.poll() is not None or not self.thread.is_alive():
                    raise ConnectionError("resource reader unavailable")
                number = next(self.ids)
                self.waiting[number] = future
                try:
                    self.process.stdin.write(_header([number, url, limit, deadline]))
                    self.process.stdin.flush()
                except BaseException:
                    self.waiting.pop(number, None)
                    raise
            try:
                status, headers, data = future.result(timeout=remaining())
            except TimeoutError as exc:
                if deadline is not None and not probe:
                    raise StartupTransferLimit(
                        "startup transfer budget exhausted"
                    ) from exc
                raise
            finally:
                with self.lock:
                    self.waiting.pop(number, None)
        finally:
            self.slots.release()
        if status == "budget":
            if probe:
                raise TimeoutError("public mirror probe deadline exceeded")
            raise StartupTransferLimit("startup transfer budget exhausted")
        if status == "tls":
            raise ssl.SSLError("public TLS validation failed")
        if status == "denied":
            raise PermissionError("resource access denied")
        if status == "unavailable":
            raise ConnectionError("public origin unavailable")
        if status == "invalid" or not isinstance(status, int):
            raise Unsupported("invalid or unauthorized public resource")
        return status, headers, data

    def read_before(self, url, limit, deadline):
        # The same child enforces the immutable URL, response type and byte policy.
        try:
            return self.request(url, limit, deadline, False)
        except StartupTransferLimit as exc:
            raise TimeoutError("interactive static read deadline exceeded") from exc


@contextmanager
def activate(archive):
    global CLIENT
    # Spawn once before the plugin process's ordinary subprocess denial is installed.
    # Both policy and reader code come from the same worker whose digest the host checked.
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() in {"SYSTEMROOT", "WINDIR", "LANG"}
    }
    with subprocess.Popen(
        [
            sys.executable,
            "-IB",
            "-c",
            "import sys;sys.path.insert(0,sys.argv[1]);from resource_broker import serve;serve()",
            str(archive),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ) as process:
        CLIENT = Reader(process)
        try:
            yield
        finally:
            CLIENT = None
            process.stdin.close()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
