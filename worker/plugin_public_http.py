"""GET policy for fixed plugin-declared public image URLs; no ambient credentials."""

import http.client
import ipaddress
from functools import lru_cache
from threading import Condition, Lock
from pathlib import Path
import re
import socket
import ssl
from time import monotonic
from urllib.parse import urlsplit

from port_errors import Unsupported


ERROR_MEDIA_TYPES = {
    "text/plain",
    "text/html",
    "application/json",
    "application/problem+json",
}
_IMAGE_URL = re.compile(
    rb"""https://[A-Za-z0-9.-]+/[A-Za-z0-9_./%~-]+\.(?:png|jpe?g|webp|gif|avif)(?=["'\s])""",
    re.I,
)


class PublishedImages:
    def __init__(self, source, plugin, *, verified_urls=None):
        if verified_urls is not None:
            self.urls = frozenset(verified_urls)
            return
        self.urls = set()
        if source is None or not plugin or not plugin.isidentifier():
            return
        root = Path(source).resolve() / plugin
        if root.resolve() != root:
            raise Unsupported("plugin image manifest outside its source")
        for path in root.rglob("*.py"):
            if path.resolve() == path:
                self.urls.update(
                    m.group().decode("ascii")
                    for m in _IMAGE_URL.finditer(path.read_bytes())
                )

    def read(self, url):
        from sayu_compat import MAX_INPUT_IMAGE_BYTES

        if url not in self.urls:
            raise Unsupported("image URL is not declared by the pinned package")
        return read_public_https(url, MAX_INPUT_IMAGE_BYTES, accept="image/*")

    def fetch(self, url):
        status, _, content = self.read(url)
        return status, content


@lru_cache(maxsize=1)
def _verified_context():
    # Each isolated reader process loads its system CA store once. All requests
    # still authenticate their own hostname and connect to a checked numeric peer.
    return ssl.create_default_context()


class _IdleConnections:
    """Only complete HTTP/1.1 responses may return a checked TLS socket."""

    def __init__(self):
        self.idle = []
        self.lock = Lock()

    def take(self, host, peers):
        with self.lock:
            for index in range(len(self.idle) - 1, -1, -1):
                until, conn = self.idle[index]
                if until <= monotonic() or conn.sock is None:
                    self.idle.pop(index)
                    conn.close()
                elif conn.host == host:
                    self.idle.pop(index)
                    try:
                        if ipaddress.ip_address(conn.sock.getpeername()[0]) in peers:
                            return conn
                    except OSError:
                        pass
                    conn.close()
        return None

    def put(self, conn):
        with self.lock:
            if len(self.idle) >= 5:
                self.idle.pop(0)[1].close()
            self.idle.append((monotonic() + 10, conn))


_mirror_connections = _IdleConnections()


class StreamControl:
    def __init__(self):
        self.lock = Condition()
        self.stopped = False
        self.sock = None
        self.credit = 8

    def take_credit(self, deadline):
        with self.lock:
            if not self.lock.wait_for(
                lambda: self.credit or self.stopped, max(0, deadline - monotonic())
            ):
                raise TimeoutError("unconsumed public stream")
            if self.stopped:
                raise InterruptedError("public stream retired")
            self.credit -= 1

    def acknowledge(self):
        with self.lock:
            self.credit = min(8, self.credit + 4)
            self.lock.notify_all()

    def bind(self, sock):
        with self.lock:
            if self.stopped:
                raise InterruptedError("public stream closed")
            self.sock = sock

    def cancel(self):
        with self.lock:
            self.stopped = True
            self.lock.notify_all()
            if self.sock is not None:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


def stream_public_https(url, mime, size, deadline, control, emit):
    """Read a previously authorized, exact-size object on a new checked connection."""

    def remaining(timeout):
        if control.stopped:
            raise InterruptedError("public stream closed")
        left = deadline - monotonic()
        if left <= 0:
            raise TimeoutError("public stream deadline exceeded")
        return min(left, timeout)

    target = urlsplit(url)
    if (
        target.scheme != "https"
        or target.netloc != target.hostname
        or target.query
        or target.fragment
    ):
        raise Unsupported("invalid public stream target")
    remaining(8)
    addresses = socket.getaddrinfo(target.hostname, 443, type=socket.SOCK_STREAM)
    peers = [ipaddress.ip_address(row[4][0]) for row in addresses]
    if not peers or any(not peer.is_global or peer.is_multicast for peer in peers):
        raise Unsupported("public stream requires a public address")
    conn = _open_checked(target.hostname, addresses, remaining)
    try:
        sock = conn.sock
        control.bind(sock)
        if ipaddress.ip_address(sock.getpeername()[0]) not in peers:
            raise Unsupported("unexpected public stream peer")
        sock.settimeout(remaining(8))
        conn.request(
            "GET", target.path, headers={"Accept": "*/*", "Accept-Encoding": "identity"}
        )
        with conn.getresponse() as response:
            # Expose actual representation metadata. No Location, Set-Cookie,
            # authentication challenge or other stateful header crosses the pipe.
            headers = [
                (k, v)
                for k, v in response.getheaders()
                if k.lower()
                in {"content-type", "content-length", "last-modified", "etag", "date"}
            ]
            if sum(len(k) + len(v) for k, v in headers) > 2048:
                raise Unsupported("public stream headers exceed limit")
            if response.status != 200:
                emit(
                    response.status,
                    [(k, v) for k, v in headers if k.lower() != "content-length"],
                    b"",
                )
                return
            if (
                response.getheader("Content-Type", "").partition(";")[0].strip().lower()
                != mime
                or response.getheader("Content-Length") != str(size)
                or response.getheader("Content-Encoding", "identity").lower()
                != "identity"
                or response.getheader("Transfer-Encoding") is not None
            ):
                raise Unsupported("public stream metadata differs from declaration")
            emit(response.status, headers, b"")
            total = 0
            while total < size:
                control.take_credit(deadline)
                sock.settimeout(remaining(8))
                chunk = response.read1(min(65536, size - total))
                if not chunk:
                    raise http.client.IncompleteRead(b"", size - total)
                total += len(chunk)
                emit("chunk", [], chunk)
    finally:
        conn.close()


def _open_checked(host, addresses, remaining):
    context = _verified_context()
    connection = http.client.HTTPSConnection(host, timeout=8, context=context)
    connection.auto_open = False
    last = None
    for family, kind, proto, _, sockaddr in addresses:
        peer = socket.socket(family, kind, proto)
        try:
            peer.settimeout(remaining(3))
            peer.connect(sockaddr)
            connection.sock = context.wrap_socket(peer, server_hostname=host)
            return connection
        except (ssl.SSLError, PermissionError):
            peer.close()
            raise
        except OSError as exc:
            peer.close()
            last = exc
    raise last


def read_public_https(
    url,
    limit,
    *,
    accept,
    skip_error_body=False,
    error_body_limit=0,
    deadline=None,
    reuse_connection=False,
):
    from resource_broker import CLIENT

    if CLIENT is not None:
        return CLIENT.read(url, limit)
    deadline = monotonic() + 15 if deadline is None else min(deadline, monotonic() + 15)

    def remaining(timeout):
        seconds = deadline - monotonic()
        if seconds <= 0:
            raise TimeoutError("public resource read deadline exceeded")
        return min(seconds, timeout)

    target = urlsplit(url)
    if (
        target.scheme != "https"
        or not target.hostname
        or target.netloc != target.hostname
        or target.query
        or target.fragment
    ):
        raise Unsupported("invalid published image URL")
    remaining(8)
    addresses = socket.getaddrinfo(target.hostname, 443, type=socket.SOCK_STREAM)
    peers = [ipaddress.ip_address(row[4][0]) for row in addresses]
    if not peers or any(not peer.is_global or peer.is_multicast for peer in peers):
        raise Unsupported("published image requires a public address")
    connection = (
        _mirror_connections.take(target.hostname, peers) if reuse_connection else None
    )
    reused = connection is not None
    try:
        while True:
            if connection is None:
                connection = _open_checked(target.hostname, addresses, remaining)
            try:
                connection.sock.settimeout(remaining(8))
                connection.request(
                    "GET",
                    target.path or "/",
                    headers={"Accept": accept, "Accept-Encoding": "identity"},
                )
                response = connection.getresponse()
                break
            except (ConnectionResetError, BrokenPipeError):
                if not reused:
                    raise
                connection.close()
                connection = None
                reused = False
        error_body = skip_error_body and response.status != 200
        if error_body:
            mime = (
                response.getheader("Content-Type", "").partition(";")[0].strip().lower()
            )
            if (
                not error_body_limit
                or not 400 <= response.status < 500
                or mime not in ERROR_MEDIA_TYPES
                or response.getheader("Content-Encoding", "identity").lower()
                != "identity"
            ):
                return response.status, [], b""
            limit = min(limit, error_body_limit)
            if response.length is not None and response.length > limit:
                return response.status, [], b""
        # Do not automatically forward a request to a new URL. sget's
        # httpx transport rechecks the exact target on every redirect.
        if response.getheader("Content-Encoding", "identity").lower() != "identity":
            raise Unsupported("published image needs identity encoding")
        body = bytearray()
        while True:
            if connection.sock is not None:
                connection.sock.settimeout(remaining(8))
            else:
                remaining(8)
            chunk = response.read1(min(65536, limit + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > limit:
                if error_body:
                    return response.status, [], b""
                raise Unsupported("published image exceeds the worker limit")
        if response.length not in (None, 0):
            raise http.client.IncompleteRead(body, response.length)
        if (
            reuse_connection
            and response.status == 200
            and not response.will_close
            and connection.sock
        ):
            _mirror_connections.put(connection)
            connection = None
        return response.status, response.getheaders(), bytes(body)
    finally:
        if connection is not None:
            connection.close()
