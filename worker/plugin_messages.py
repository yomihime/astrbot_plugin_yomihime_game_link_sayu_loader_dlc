"""Plugin wire types for the GsCore 0.12.2 adapter protocol.

Field order, defaults and names follow the pinned protocol (GPL-3.0).
"""

import asyncio
from typing import Any, Literal, TypeVar, Union

from msgspec import Struct


class Message(Struct):
    type: str | None = None
    data: Any = None


class MessageReceive(Struct):
    bot_id: str = "Bot"
    bot_self_id: str = ""
    msg_id: str = ""
    user_type: Literal["group", "direct", "channel", "sub_channel"] = "group"
    group_id: str | None = None
    user_id: str = ""
    sender: dict[str, Any] = {}
    user_pm: int = 6
    content: list[Message] = []


class Event(MessageReceive):
    WS_BOT_ID: str | None = None
    task_id: str = ""
    task_event: asyncio.Event | None = None
    real_bot_id: str = ""
    raw_text: str = ""
    command: str = ""
    text: str = ""
    image: str | None = None
    image_list: list[Any] = []
    image_id: str | None = None
    image_id_list: list[str] = []
    reply_image_id_list: list[str] = []
    audio_id: str | None = None
    audio_id_list: list[str] = []
    video_id: str | None = None
    video_id_list: list[str] = []
    at: str | None = None
    at_list: list[Any] = []
    is_tome: bool = False
    at_bot: bool = False
    quote_auto_at: bool = False
    reply: str | None = None
    reply_id: str | None = None
    node: list[Message] | None = None
    file_name: str | None = None
    file: str | None = None
    file_type: Literal["url", "base64"] | None = None
    regex_group: tuple[str, ...] = ()
    regex_dict: dict[str, str] = {}
    meta_event_type: str | None = None
    meta_event_data: dict[str, Any] = {}

    def _identity(self):
        return (
            self.WS_BOT_ID,
            self.bot_id,
            self.bot_self_id,
            self.user_id,
            self.group_id,
            self.user_type,
        )

    def __eq__(self, other):
        if not isinstance(other, Event):
            return NotImplemented
        return self._identity() == other._identity()

    def __hash__(self):
        return hash(self._identity())

    @property
    def session_id(self):
        connection = self.WS_BOT_ID or self.real_bot_id or self.bot_id or "0"
        direct = self.user_type == "direct"
        identity = self.user_id if direct else self.group_id
        return f"{connection}:{self.bot_id or '0'}:{self.bot_self_id or '0'}:{'private' if direct else 'group'}:{identity or '0'}"

    def get_meta(self, key, default=None):
        return self.meta_event_data.get(key, default)


class MessageSend(Struct):
    bot_id: str = "Bot"
    bot_self_id: str = ""
    msg_id: str = ""
    target_type: str | None = None
    target_id: str | None = None
    content: list[Message] | None = None
    echo: str | None = None


class Button(Struct):
    text: str
    data: str
    pressed_text: str | None = None
    style: Literal[0, 1] = 1
    action: Literal[-1, 0, 1, 2] = -1
    permisson: Literal[0, 1, 2, 3] = 2
    specify_role_ids: list[str] = []
    specify_user_ids: list[str] = []
    unsupport_tips: str = "您的客户端暂不支持该功能, 请升级后适配..."
    prefix: str = ""
    _edited: bool = False


T = TypeVar("T", bound=Button)
ButtonList = Union[list[T], list[list[T]]]
ButtonType = Union[list[str], list[T], list[list[str]], list[list[T]]]
