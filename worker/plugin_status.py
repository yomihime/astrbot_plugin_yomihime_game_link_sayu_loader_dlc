"""Plugin status callbacks and native broadcast message types; generation-local state."""

from collections.abc import Awaitable, Callable
from typing import TypedDict

from PIL import Image


class PluginStatus(TypedDict):
    icon: Image.Image
    status: dict[str, Callable[..., Awaitable[str | int | float]]]


def install_status(package):
    from gsuid_core.models import Message

    class BoardCastMsg(TypedDict):
        bot_id: str
        messages: list[Message]

    class BoardCastMsgDict(TypedDict):
        private_msg_dict: dict[str, list[BoardCastMsg]]
        group_msg_dict: dict[str, BoardCastMsg]

    status = {}

    def register_status(ICON, plugin_name, plugin_status):
        status[plugin_name] = {"icon": ICON, "status": plugin_status}

    package("gsuid_core.status")
    package("gsuid_core.status.models").PluginStatus = PluginStatus
    module = package("gsuid_core.status.plugin_status")
    module.plugins_status = status
    module.register_status = register_status
    package("gsuid_core.utils.boardcast")
    types = package("gsuid_core.utils.boardcast.models")
    types.BoardCastMsg = BoardCastMsg
    types.BoardCastMsgDict = BoardCastMsgDict
