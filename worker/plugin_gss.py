"""GsCore's per-process server registry for host-owned adapter connections."""

import asyncio
import inspect
from types import MappingProxyType

from port_errors import Unsupported


class _ServerRegistry:
    def __init__(self):
        self._bots = {}
        self._sockets = {}
        self.active_bot = MappingProxyType(self._bots)
        self.active_ws = MappingProxyType(self._sockets)
        self.bot_connect_def = set()
        self.is_initialized = True
        self.is_load = False

    def on_bot_connect(self, func):
        self.bot_connect_def.difference_update(
            [
                old
                for old in self.bot_connect_def
                if (old.__module__, old.__name__) == (func.__module__, func.__name__)
            ]
        )
        self.bot_connect_def.add(func)
        return func

    async def send(self, message, bot_id):
        adapter = self._sockets.get(bot_id)
        if adapter is not None:
            await adapter.send_text(message)

    async def broadcast(self, message):
        await asyncio.gather(
            *(self.send(message, name) for name in tuple(self._sockets))
        )

    async def load_plugins(self, dev_mode=False):
        raise Unsupported("package lifecycle is managed by the host")


class AdapterRegistry:
    """Only a host-owned connection can register a live transport; package code gets a view."""

    def __init__(self):
        self.server = _ServerRegistry()

    async def attach(self, bot_id, bot, transport):
        self.server._bots[bot_id] = bot
        self.server._sockets[bot_id] = transport
        tasks = []
        for callback in tuple(self.server.bot_connect_def):
            if inspect.iscoroutinefunction(callback):
                tasks.append(callback())
            else:
                callback()
        if tasks:
            await asyncio.gather(*tasks)

    def detach(self, bot_id, transport):
        if self.server._sockets.get(bot_id) is transport:
            self.server._sockets.pop(bot_id)
            self.server._bots.pop(bot_id)


def install_gss(package, server_module):
    registry = AdapterRegistry()
    instance = registry.server

    class GsServer:
        on_bot_connect = staticmethod(instance.on_bot_connect)

        def __new__(cls):
            return instance

    async def load_gss(dev_mode=False):
        if not instance.is_load:
            await instance.load_plugins(dev_mode)

    server_module.GsServer = GsServer
    module = package("gsuid_core.gss")
    module.gss, module.load_gss = instance, load_gss
    return registry
