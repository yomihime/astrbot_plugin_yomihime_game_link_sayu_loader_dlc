"""Private, invocation-scoped dispatch for native account intake services."""

import asyncio
from importlib import import_module
from pathlib import Path
import sys

from plugin_bindings import _current
from port_errors import Unsupported


class IssuerUnavailable(Unsupported):
    def __init__(self):
        super().__init__("account intake requires a trusted host issuer")


class _CookieIssuer:
    """Native issuer contract. No authenticated host transport is available."""

    __slots__ = ()

    async def get_cookie_token_by_stoken(
        self, stoken, mys_id, full_sk=None, is_os=False
    ):
        raise IssuerUnavailable()

    async def get_stoken_by_login_ticket(self, lt, mys_id, is_os=None):
        raise IssuerUnavailable()

    async def get_mihoyo_bbs_info(self, mys_id, cookie=None, is_os=False):
        raise IssuerUnavailable()

    async def ck_in_new_device(self, uid, app_cookie=None, game_name=None):
        raise IssuerUnavailable()

    @staticmethod
    def get_overseas_device_id(account_id):
        from plugin_mys import BaseMysApi

        return BaseMysApi.get_overseas_device_id(account_id)

    @staticmethod
    def get_overseas_device_fp(account_id):
        from plugin_mys import BaseMysApi

        return BaseMysApi.get_overseas_device_fp(account_id)


intake_issuer = _CookieIssuer()


def require_account_owner(bot_id, user_id):
    owner = _current.get()
    if (
        owner is None
        or not owner.active
        or not owner.account_records
        or owner.task is not asyncio.current_task()
        or (owner.adapter, owner.actor) != (bot_id, user_id)
    ):
        raise Unsupported("account intake requires the current private account owner")


def install_account_services(package, registration):
    namespace = "gsuid_core.buildin_plugins"
    package(namespace, Path(__file__).parent / "native_account_services")
    plugin = "core_command"
    module = namespace + "." + plugin
    # A fresh runtime must replay decorators, including in same-process tests.
    for name in (module + ".user_login", module):
        sys.modules.pop(name, None)
    with registration(plugin):
        import_module(module + ".user_login")
