"""Profile-scoped, read-only enumeration of an explicitly supplied account inventory."""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass

import plugin_bindings
from plugin_mys import _owns_task


class EnumerationUnavailable(NotImplementedError):
    """The current delivery cannot enumerate this profile's raw account records."""


_current = ContextVar("account_enumeration", default=None)
_installed = None


@dataclass
class _Lease:
    authority: object
    owner: plugin_bindings.Owner
    active: bool = True


class ProfileInventory:
    def __init__(self, admins, read_rows):
        self._admins = frozenset(admins)
        self._read_rows = read_rows
        self._generation = object()

    def _owner(self):
        owner = plugin_bindings._current.get()
        if (
            self._generation is not _installed
            or self._read_rows is None
            or not owner
            or not owner.active
            or not owner.writable
            or not _owns_task(owner)
            or (owner.actor, owner.adapter) not in self._admins
        ):
            raise EnumerationUnavailable(
                "profile account inventory requires an authorized private administrator delivery"
            )
        return owner

    @contextmanager
    def delivery(self):
        """Enter from the trusted adapter only after authorizing the current delivery."""
        lease = _Lease(self, self._owner())
        token = _current.set(lease)
        try:
            yield
        finally:
            lease.active = False
            _current.reset(token)

    def rows(self, table, model):
        owner = self._owner()
        lease = _current.get()
        if (
            not lease
            or not lease.active
            or lease.authority is not self
            or lease.owner is not owner
        ):
            raise EnumerationUnavailable(
                "no active administrator grant for this profile"
            )
        result = []
        for values in self._read_rows(table):
            if issubclass(model, plugin_bindings.GsBind):
                row = model._from_storage(deepcopy(values))
            else:
                row = model()
                vars(row).update(deepcopy(values))
            result.append(row)
        return result


def install_enumeration(models, *, admins=(), read_rows=None):
    """Install ordinary model methods. Only the caller receives the grant issuer.

    A trusted inventory provider returns complete row mappings from the bound
    profile. Without a provider and an administrator allowlist no rows can be read.
    """
    global _installed
    authority = ProfileInventory(admins, read_rows)

    class BindingQueries:
        @classmethod
        async def get_all_data(cls):
            return authority.rows("bindings", cls)

        @classmethod
        async def get_all_uid_list_by_game(cls, bot_id, game_name=None):
            rows = await cls.get_all_data()
            result = []
            for row in rows:
                if row.bot_id == bot_id:
                    value = getattr(row, cls.get_gameid_name(game_name))
                    if value:
                        result.extend(value.split("_"))
            return result

    class InventoryQueries:
        @classmethod
        async def get_all_data(cls):
            return authority.rows("users", cls)

        @classmethod
        async def get_all_user(cls, without_error=True):
            return [
                row
                for row in await cls.get_all_data()
                if row.cookie and (not without_error or row.status is None)
            ]

        @classmethod
        async def get_all_cookie(cls):
            return [row.cookie for row in await cls.get_all_user()]

        @classmethod
        async def get_all_stoken(cls):
            return [row.stoken for row in await cls.get_all_user() if row.stoken]

        @classmethod
        async def get_all_error_cookie(cls):
            # The pinned core first filters to valid users, then checks status.
            return [row.cookie for row in await cls.get_all_user() if row.status]

        @classmethod
        async def get_all_push_user_list(cls):
            return await cls.get_push_user_list()

        @classmethod
        async def get_all_sign_user_list(cls):
            return await cls.get_sign_user_list()

        @classmethod
        async def get_push_user_list(cls, push_title=None):
            column = (
                "push_switch" if push_title is None else f"{push_title}_push_switch"
            )
            return [
                row for row in await cls.get_all_user() if getattr(row, column) != "off"
            ]

        @classmethod
        async def get_sign_user_list(cls, sign_title=None):
            column = (
                "sign_switch" if sign_title is None else f"{sign_title}_sign_switch"
            )
            return [
                row for row in await cls.get_all_user() if getattr(row, column) != "off"
            ]

    # SQLAlchemy must keep the original mapped class for row hydration and column
    # expressions. Only replace its profile-wide queries with the admin port.
    for name, queries in (("GsBind", BindingQueries), ("GsUser", InventoryQueries)):
        model = getattr(models, name)
        if hasattr(model, "__table__"):
            for method_name, method in vars(queries).items():
                if isinstance(method, classmethod):
                    setattr(model, method_name, method)
        else:
            setattr(models, name, type(name, (queries, model), {}))
    _installed = authority._generation
    return authority
