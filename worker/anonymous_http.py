"""Worker-side request channel, owned by one running handler task."""

import asyncio
import base64
import binascii
from contextlib import contextmanager
from contextvars import ContextVar
import json
import re
import sys

from port_errors import Unsupported


_active = ContextVar("anonymous_read_invocation", default=None)


@contextmanager
def read_scope(nonce, write):
    if not isinstance(nonce, str) or re.fullmatch("[0-9a-f]{32}", nonce) is None:
        raise ValueError("invalid anonymous read invocation")
    lease = [asyncio.current_task(), nonce, write, 0]
    token = _active.set(lease)
    try:
        yield
    finally:
        lease[0] = None
        _active.reset(token)


async def get(source, timeout):
    from anonymous_sources import MAX_BODY

    lease = _active.get()
    if lease is None or lease[0] is not asyncio.current_task():
        raise Unsupported("anonymous HTTP requires the live invocation")
    _, nonce, write, sequence = lease
    values = [4.0, *(v for v in timeout.values() if v is not None)]
    if any(type(v) not in (float, int) or not 0 < v for v in values):
        raise Unsupported("invalid source timeout")
    budget = int(min(values) * 1000)
    if budget < 1:
        raise Unsupported("public source timeout elapsed")
    sequence += 1
    lease[3] = sequence
    write(
        {
            "get": source,
            "invocation": nonce,
            "seq": sequence,
            "timeout_ms": budget,
        }
    )
    reading = asyncio.create_task(
        asyncio.to_thread(sys.stdin.buffer.readline, MAX_BODY * 2)
    )
    try:
        line = await asyncio.shield(reading)
    except asyncio.CancelledError:
        # Retire this grant and consume its pending frame before a subsequent
        # callback can read stdin. Host cancellation aborts the whole generation.
        lease[0] = None
        await asyncio.shield(reading)
        raise
    if not line.endswith(b"\n"):
        raise Unsupported("invalid anonymous response")
    try:
        frame = json.loads(line)
        if (
            isinstance(frame, dict)
            and frame.get("source") == source
            and frame.get("invocation") == nonce
            and frame.get("seq") == sequence
            and "error" in frame
        ):
            from httpx import RequestError, TimeoutException

            code = frame["error"]
            if code == "timeout":
                raise TimeoutException("anonymous source timeout")
            # Source errors are finite, sanitized SDK codes. Do not relay server messages.
            status = frame.get("status")
            raise RequestError(
                "anonymous source unavailable"
                + (
                    f" (HTTP {status})"
                    if type(status) is int and 100 <= status <= 599
                    else ""
                )
            )
        if set(frame) != {"source", "invocation", "seq", "body"} or (
            frame["source"] != source
            or frame["invocation"] != nonce
            or frame["seq"] != sequence
        ):
            raise Unsupported("anonymous response unavailable")
        body = base64.b64decode(frame["body"], validate=True)
    except (TypeError, ValueError, binascii.Error):
        raise Unsupported("anonymous response unavailable") from None
    if not 0 < len(body) <= MAX_BODY:
        raise Unsupported("invalid anonymous response size")
    return body
