"""GET policy for fixed plugin-declared public image URLs; no ambient credentials."""

import http.client
import ipaddress
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
    def __init__(self, source, plugin):
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


def read_public_https(
    url, limit, *, accept, skip_error_body=False, error_body_limit=0, deadline=None
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
    connection = http.client.HTTPSConnection(target.hostname, timeout=8)
    try:
        # Connect to an already checked numeric peer. TLS still validates
        # the publisher's DNS name and sends that name as SNI.
        last = None
        for family, kind, proto, _, sockaddr in addresses:
            peer = socket.socket(family, kind, proto)
            try:
                peer.settimeout(remaining(3))
                peer.connect(sockaddr)
                connection.sock = ssl.create_default_context().wrap_socket(
                    peer, server_hostname=target.hostname
                )
                break
            except (ssl.SSLError, PermissionError):
                peer.close()
                raise
            except OSError as exc:
                peer.close()
                last = exc
        else:
            raise last
        connection.sock.settimeout(remaining(8))
        connection.request(
            "GET",
            target.path or "/",
            headers={"Accept": accept, "Accept-Encoding": "identity"},
        )
        response = connection.getresponse()
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
        return response.status, response.getheaders(), bytes(body)
    finally:
        connection.close()
