"""A target port bound to one recipient by its caller. No host port is installed."""

import base64
from dataclasses import dataclass
from typing import Awaitable, Callable


class UnsupportedMessage(ValueError):
    """The host cannot deliver this reply without losing content."""


MAX_FILE_BYTES = 1024 * 1024


def file_payload(content, name):
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 255
        or name in {".", ".."}
        or any(char in "/\\:|" or ord(char) < 32 for char in name)
    ):
        raise UnsupportedMessage("invalid attachment name")
    if not isinstance(content, (bytes, bytearray)) or len(content) > MAX_FILE_BYTES:
        raise UnsupportedMessage("only bounded in-memory attachments are supported")
    return name + "|" + base64.b64encode(content).decode("ascii")


@dataclass(frozen=True)
class BoundTarget:
    kind: str
    recipient: str
    # The transport selects the actual recipient when constructing this
    # callback. No caller-provided destination is forwarded to it.
    emit: Callable[[list], Awaitable[None]]

    async def send(self, kind, recipient, message):
        if (kind, recipient) != (self.kind, self.recipient) or not recipient:
            raise UnsupportedMessage("recipient is outside the bound target")
        from sayu_compat import Message

        items = message if isinstance(message, list) else [message]
        if not 1 <= len(items) <= 32:
            raise UnsupportedMessage("invalid targeted message size")
        parts = []
        text_length = file_bytes = 0
        for item in items:
            if isinstance(item, str):
                item = Message("text", item)
            if not isinstance(item, Message) or not isinstance(item.data, str):
                raise UnsupportedMessage("unsupported targeted message part")
            if item.type == "text":
                text_length += len(item.data)
                if (
                    not item.data.strip()
                    or text_length > 4096
                    or item.data.startswith(("base64://", "file://"))
                ):
                    raise UnsupportedMessage("unsupported targeted text")
            elif item.type == "file":
                name, sep, encoded = item.data.partition("|")
                if not sep or len(encoded) > 4 * (
                    (MAX_FILE_BYTES - file_bytes + 2) // 3
                ):
                    raise UnsupportedMessage("invalid targeted attachment")
                try:
                    raw = base64.b64decode(encoded, validate=True)
                except ValueError as exc:
                    raise UnsupportedMessage("invalid targeted attachment") from exc
                file_bytes += len(raw)
                if file_bytes > MAX_FILE_BYTES or item.data != file_payload(raw, name):
                    raise UnsupportedMessage("invalid targeted attachment")
            else:
                raise UnsupportedMessage("unsupported targeted segment")
            parts.append({"type": item.type, "data": item.data})
        await self.emit(parts)
