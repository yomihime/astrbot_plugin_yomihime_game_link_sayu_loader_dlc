import asyncio
from contextlib import contextmanager
from http.client import IncompleteRead
from pathlib import Path
import socket
import ssl
import sys
from time import monotonic
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import MagicMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
from plugin_public_http import StreamControl, stream_public_https
from port_errors import Unsupported
from resource_broker import allowed, stream_spec
from resource_httpx import static_httpx, stream_scope

URL = "https://mirror.example.org/Fixture/speedtest"
MIME = "application/octet-stream"
PROFILE = SimpleNamespace(
    PACKAGE="Fixture",
    STATIC_RESOURCE_URLS=(),
    PUBLIC_IMAGE_ORIGINS=(),
    PUBLIC_BULK_MIRRORS=("https://mirror.example.org",),
    STATIC_RESOURCE_DIRECTORIES=(),
    STATIC_STREAM_OBJECTS=((URL, MIME, 4),),
)


class BodyReadTests(TestCase):
    @contextmanager
    def transport(self, status=200, **overrides):
        headers = {
            "Content-Length": "4",
            "Content-Type": MIME,
            "Set-Cookie": "ignored=1",
            "ETag": "abc",
            **overrides,
        }
        conn = MagicMock()
        conn.sock.getpeername.return_value = ("8.8.8.8", 443)
        response = conn.getresponse.return_value.__enter__.return_value
        response.getheaders.return_value = list(headers.items())
        response.getheader.side_effect = headers.get
        response.status = status
        response.read1.side_effect = [b"a", b"bc", b"d"]
        addr = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        with (
            patch("plugin_public_http.socket.getaddrinfo", return_value=addr),
            patch("plugin_public_http._open_checked", return_value=conn),
        ):
            yield conn, response

    def run_read(self, control, emit):
        stream_public_https(URL, MIME, 4, monotonic() + 1, control, emit)

    def test_only_declared_stream_peer_and_operation(self):
        self.assertEqual(stream_spec(URL, PROFILE), (MIME, 4))
        for url in (
            URL + "/other",
            URL + "?v=1",
            URL.replace("Fixture", "Other"),
            URL.replace("mirror", "other"),
        ):
            with self.assertRaises(Unsupported):
                stream_spec(url, PROFILE)
        with self.assertRaises(Unsupported):
            allowed(URL, PROFILE)
        for peer in ("127.0.0.1", "10.0.0.1", "::1"):
            with (
                patch(
                    "plugin_public_http.socket.getaddrinfo",
                    return_value=[(0, 0, 0, "", (peer, 443))],
                ),
                patch("plugin_public_http._open_checked") as connect,
            ):
                with self.assertRaises(Unsupported):
                    self.run_read(StreamControl(), lambda *_: None)
                connect.assert_not_called()
        with self.transport() as (conn, _):
            conn.sock.getpeername.return_value = ("8.8.4.4", 443)
            with self.assertRaises(Unsupported):
                self.run_read(StreamControl(), lambda *_: None)
            conn.request.assert_not_called()
        with (
            self.transport(),
            patch("plugin_public_http._open_checked", side_effect=ssl.SSLError),
        ):
            with self.assertRaises(ssl.SSLError):
                self.run_read(StreamControl(), lambda *_: None)

    def test_headers_precede_arriving_bytes_and_partial_cancel(self):
        with self.transport() as (conn, response):
            frames = []
            control = StreamControl()

            def observe(status, headers, data):
                frames.append((status, dict(headers), data, response.read1.call_count))
                if data == b"bc":
                    control.cancel()

            with self.assertRaises(InterruptedError):
                self.run_read(control, observe)
            self.assertEqual(
                frames,
                [
                    (
                        200,
                        {"Content-Length": "4", "Content-Type": MIME, "ETag": "abc"},
                        b"",
                        0,
                    ),
                    ("chunk", {}, b"a", 1),
                    ("chunk", {}, b"bc", 2),
                ],
            )
            conn.sock.shutdown.assert_called_once_with(socket.SHUT_RDWR)
            conn.close.assert_called_once()

    def test_denial_redirect_encoding_size_and_incomplete_body(self):
        for status in (302, 403, 404, 500):
            with self.transport(status, Location="https://unrelated.invalid/") as (
                conn,
                response,
            ):
                frames = []
                self.run_read(StreamControl(), lambda *args: frames.append(args))
                self.assertEqual(len(frames), 1)
                self.assertEqual(frames[0][0], status)
                self.assertNotIn("Location", dict(frames[0][1]))
                response.read1.assert_not_called()
                self.assertEqual(conn.request.call_count, 1)
        for invalid in (
            {"Content-Type": "text/html"},
            {"Content-Length": "5"},
            {"Content-Encoding": "gzip"},
            {"Transfer-Encoding": "chunked"},
        ):
            with (
                self.transport(**invalid) as (_, response),
                self.assertRaises(Unsupported),
            ):
                self.run_read(StreamControl(), lambda *_: None)
                response.read1.assert_not_called()
        with self.transport() as (_, response):
            response.read1.side_effect = [b"ab", b""]
            with self.assertRaises(IncompleteRead):
                self.run_read(StreamControl(), lambda *_: None)

    def test_slow_consumer_credit_and_cancellation(self):
        control = StreamControl()
        for _ in range(8):
            control.take_credit(monotonic() + 1)
        with self.assertRaises(TimeoutError):
            control.take_credit(monotonic())
        control.acknowledge()
        for _ in range(4):
            control.take_credit(monotonic() + 1)
        control.cancel()
        with self.assertRaises(InterruptedError):
            control.take_credit(monotonic() + 1)


class HttpxStreamTests(IsolatedAsyncioTestCase):
    async def test_callback_lease_order_and_aclose(self):
        class Channel:
            def __init__(self):
                self.messages = asyncio.Queue()
                for item in [
                    (200, [("Content-Type", MIME)], b""),
                    ("chunk", [], b"ab"),
                ]:
                    self.messages.put_nowait(item)
                self.cancelled = False

            async def next(self):
                return await self.messages.get()

            def cancel(self):
                self.cancelled = True
                self.messages.put_nowait(("unavailable", [], b""))

        reader = MagicMock()
        channel = Channel()
        reader.stream.return_value = channel
        with static_httpx(PROFILE, reader):
            async with httpx.AsyncClient() as client:
                with self.assertRaises(Unsupported):
                    await client.get(URL)
                with stream_scope():
                    for kwargs in (
                        {"headers": {"Authorization": "x"}},
                        {"headers": {"Cookie": "x=1"}},
                    ):
                        with self.assertRaises(Unsupported):
                            await client.get(URL, **kwargs)
                    with self.assertRaises(Unsupported):
                        await client.post(URL)
                    gate = asyncio.Event()

                    async def late_request():
                        await gate.wait()
                        return await client.get(URL)

                    late = asyncio.create_task(late_request())
                    resp = await client.send(
                        client.build_request("GET", URL), stream=True
                    )
                    seq = resp.aiter_bytes()
                    self.assertEqual(await seq.__anext__(), b"ab")
                    pending = asyncio.create_task(seq.__anext__())
                    await asyncio.sleep(0)
                    self.assertFalse(pending.done())
                gate.set()
                self.assertTrue(channel.cancelled)
                with self.assertRaises(Unsupported):
                    await pending
                with self.assertRaises(Unsupported):
                    await late
                reader.stream.assert_called_once()
                await resp.aclose()
            self.assertEqual(reader.static_httpx[-1]["bytes"], 2)
