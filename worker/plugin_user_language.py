"""Resolve the public or owner-scoped language of one adapter event."""

import asyncio

import plugin_bindings
from port_errors import Unsupported


def require_owner(user_id, bot_id=None, *, writable=False):
    owner = plugin_bindings._current.get()
    if (
        owner is None
        or not owner.active
        or owner.task is not asyncio.current_task()
        or owner.actor != user_id
        or (bot_id is not None and owner.adapter != bot_id)
        or (writable and not owner.writable)
    ):
        raise Unsupported("user language requires an active matching owner grant")
    return owner


async def bot_get_lang(self):
    from gsuid_core.i18n import get_lang

    owner = plugin_bindings._current.get()
    if owner is None or not owner.active:
        # A public delivery has no private preferences. Never replay a cached
        # private language after the event grant expires.
        return get_lang()
    require_owner(self.ev.user_id, self.ev.bot_id)
    if getattr(self, "_lang_owner", None) is not owner or self._lang is None:
        from gsuid_core.utils.database.models import CoreUser

        self._lang = await CoreUser.get_user_lang(self.ev.user_id)
        self._lang_owner = owner
    return self._lang


async def bot_t(self, key, /, **params):
    from gsuid_core.i18n import t

    return t(key, lang=await self.get_lang(), **params)
