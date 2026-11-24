"""Decode frames from an isolated GsCore adapter into the existing plugin wire types.

Protocol semantics follow gsuid_core.handler and gsuid_core.models at 96cd7353
(GPL-3.0, see licenses/gsuid_core-COPYING.txt).
"""

from dataclasses import dataclass
from uuid import uuid4

from msgspec import DecodeError, json
from plugin_messages import Event, Message, MessageReceive


NODE_MARK = "[合并转发]"
_MEDIA_LABELS = {
    "image": "[图片]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
}


@dataclass(frozen=True)
class TrustedAdapterRoles:
    """Role configuration supplied by the owner of an authenticated test adapter."""

    masters: frozenset[str] = frozenset()
    superusers: frozenset[str] = frozenset()

    def effective(self, user_id, claimed):
        if user_id in self.masters:
            return 0
        if user_id in self.superusers:
            return 1
        return claimed if claimed >= 1 else 2


class EventResources:
    """Keep references in memory for one delivery, without following URLs or file paths."""

    def __init__(self):
        self._payloads = {}

    def register(self, prefix, payload):
        resource_id = f"{prefix}_{uuid4().hex[:16]}"
        self._payloads[resource_id] = payload
        return resource_id

    def peek(self, resource_id):
        return self._payloads[resource_id]


def decode_inbound_frame(data, bot_id):
    try:
        return json.decode(data, type=MessageReceive)
    except DecodeError:
        return None


def normalize_node_items(data, depth=0):
    items = []
    if not isinstance(data, list):
        return items
    for entry in data:
        if isinstance(entry, dict):
            kind = entry.get("type")
            entry = Message(str(kind) if kind is not None else None, entry.get("data"))
        if not isinstance(entry, Message):
            continue
        if entry.type == "node":
            items.append(Message("text", NODE_MARK))
            if depth < 2:
                items.extend(normalize_node_items(entry.data, depth + 1))
        else:
            items.append(entry)
    return items


def format_node_preview(items, depth=0):
    lines = [NODE_MARK]
    for item in items:
        if item.type == "text" and item.data is not None:
            value = str(item.data).strip()
            if value:
                lines.append(value)
        elif item.type in _MEDIA_LABELS:
            lines.append(_MEDIA_LABELS[item.type])
        elif item.type == "node":
            if depth < 2:
                lines.append(
                    format_node_preview(normalize_node_items(item.data), depth + 1)
                )
            else:
                lines.append(NODE_MARK)
    return "\n".join(lines)


def msg_process(message, resources):
    event = Event(
        message.bot_id.partition(":")[0],
        message.bot_self_id,
        message.msg_id,
        message.user_type,
        message.group_id,
        message.user_id,
        message.sender,
        message.user_pm,
        real_bot_id=message.bot_id,
        is_tome=message.user_type == "direct",
    )
    saw_body = saw_text = saw_reply = late_mention = False
    bot_mentions = 0
    before, after = [], []
    for part in message.content:
        kind, value = part.type, part.data
        if kind == "text":
            if not value:
                continue
            text = str(value).strip()
            if text:
                saw_text = saw_body = True
            event.text += text
            event.raw_text += text
        elif kind == "at":
            saw_body = True
            if str(value) == str(event.bot_self_id):
                event.at_bot = event.is_tome = True
                bot_mentions += 1
                late_mention |= saw_text
                continue
            event.at = str(value)
            event.at_list.append(event.at)
        elif kind == "image":
            event.image = value
            if value:
                event.image_list.append(value)
                event.image_id = resources.register("img", value)
                event.image_id_list.append(event.image_id)
                if saw_reply:
                    (after if saw_body else before).append(event.image_id)
                else:
                    saw_body = True
        elif kind in ("record", "video"):
            saw_body = True
            if value and kind == "record":
                event.audio_id = resources.register("aud", value)
                event.audio_id_list.append(event.audio_id)
            elif value:
                event.video_id = resources.register("vid", value)
                event.video_id_list.append(event.video_id)
        elif kind == "reply":
            saw_reply = True
            event.reply = value
        elif kind == "reply_id" and value is not None:
            saw_reply = True
            event.reply_id = str(value)
        elif kind == "node":
            event.node = normalize_node_items(value)
        elif kind == "file" and value:
            fields = value.split("|")
            event.file_name, event.file = fields[0], fields[1]
            event.file_type = "url" if str(event.file).startswith("http") else "base64"
        elif kind and kind.startswith("meta-"):
            event.meta_event_type = kind[5:]
            if isinstance(value, dict):
                event.meta_event_data = value
                for key in ("user_id", "group_id"):
                    if not getattr(event, key) and value.get(key) is not None:
                        setattr(event, key, str(value[key]))
        event.content.append(part)
    event.quote_auto_at = bool(
        bot_mentions == 1
        and (event.reply_id or event.reply)
        and not late_mention
        and not event.at_list
    )
    event.reply_image_id_list = after or (before if saw_body else [])
    if event.node is not None and (event.reply is not None or event.reply_id):
        if not event.reply:
            event.reply = format_node_preview(event.node)
        elif NODE_MARK not in str(event.reply):
            event.reply = f"{NODE_MARK}\n{event.reply}"
    return event


def decode_adapter_event(frame, *, connection_id, resources, roles=None):
    """Use only on a dedicated adapter connection; missing role authority defaults to 6."""
    message = decode_inbound_frame(frame, connection_id)
    if message is None:
        return None
    event = msg_process(message, resources)
    event.WS_BOT_ID = connection_id
    event.user_pm = (
        roles.effective(event.user_id, message.user_pm) if roles is not None else 6
    )
    return event
