"""Serve anonymous worker reads inside one current bound SDK callback."""

import asyncio
import base64

from .. import _api_v2 as ygl
from .anonymous_sources import MAX_BODY, SOURCES, valid_body


class AnonymousReads:
    def __init__(self, profile, nonce, http):
        self.specs = SOURCES[profile]
        self.nonce = nonce
        self.http = http
        self.sequence = 0

    async def reply(self, frame):
        # A malformed or stale frame is a stream error. The outer exchange
        # retires that worker without replaying the request.
        if (
            set(frame) != {"get", "invocation", "seq", "timeout_ms"}
            or frame["invocation"] != self.nonce
        ):
            raise ValueError("invalid anonymous read frame")
        source = frame["get"]
        if not isinstance(source, str) or source not in self.specs:
            raise ValueError("undeclared anonymous read")
        budget = frame["timeout_ms"]
        if type(budget) is not int or not 1 <= budget <= 4000:
            raise ValueError("invalid source timeout")
        sequence = frame["seq"]
        if type(sequence) is not int or sequence != self.sequence + 1:
            raise ValueError("stale anonymous read")
        self.sequence = sequence
        spec = self.specs[source]
        _, path, query, shape = spec
        result = {
            "source": source,
            "invocation": self.nonce,
            "seq": sequence,
            "error": "unavailable",
        }
        try:
            async with asyncio.timeout(budget / 1000):
                response = await self.http.fetch(
                    ygl.HttpRequest(source, path, query=query)
                )
        except TimeoutError:
            return {**result, "error": "timeout"}
        except ygl.SourceHttpError as exc:
            return {**result, "error": exc.code, "status": exc.status_code}
        except (ygl.AccessDenied, ygl.ServiceUnavailable):
            return result
        if (
            response.status_code != 200
            or type(response.body) is not bytes
            or not 0 < len(response.body) <= MAX_BODY
            or not valid_body(shape, response.body)
            or response.final_url != f"https://{spec[0]}{path}"
        ):
            return result
        return {
            "source": source,
            "invocation": self.nonce,
            "seq": sequence,
            "body": base64.b64encode(response.body).decode("ascii"),
        }
