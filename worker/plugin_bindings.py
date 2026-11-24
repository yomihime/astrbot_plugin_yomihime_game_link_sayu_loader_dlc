"""Owner-scoped UID bindings in the profile's private SQLite database."""

from contextlib import closing, contextmanager
import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
import re
import sqlite3
from types import SimpleNamespace


@dataclass
class Owner:
    actor: str
    adapter: str
    writable: bool
    active: bool = True
    task: asyncio.Task | None = None
    preferences: bool = False
    uid_read: bool = True
    uid_write: bool = False


_current = ContextVar("binding_owner", default=None)
_COLUMNS = ("uid", "sr_uid", "bb_uid", "bbb_uid", "zzz_uid", "wd_uid")


@contextmanager
def binding_scope(
    actor,
    adapter,
    private,
    *,
    writable=False,
    preferences=False,
    uid_read=True,
    uid_write=False,
):
    owner = Owner(
        actor,
        adapter,
        writable,
        bool(private and actor and adapter),
        preferences=preferences,
        uid_read=uid_read,
        uid_write=uid_write,
    )
    try:
        owner.task = asyncio.current_task()
    except RuntimeError:
        pass  # Synchronous entry has no owning event-loop task.
    token = _current.set(owner)
    try:
        yield
    finally:
        owner.active = False
        _current.reset(token)


async def get_uid(
    bot, ev, bind_model, game_name=None, get_user_id=False, pattern=r"\d+"
):
    matches = re.findall(pattern, ev.text) if pattern else []
    actor = ev.at or ev.user_id
    uid = (
        matches[0]
        if matches
        else await bind_model.get_uid_by_game(actor, ev.bot_id, game_name)
    )
    return (uid, actor) if get_user_id else uid


@contextmanager
def _transaction(model, user_id, bot_id, *, write=False, uid_write=False):
    owner = _current.get()
    if (
        not owner
        or not owner.active
        or owner.task is None
        or owner.task is not asyncio.current_task()
        or not owner.uid_read
        or user_id != owner.actor
        or bot_id != owner.adapter
    ):
        raise NotImplementedError("bindings require the current owner delivery")
    if write and not (owner.writable or (uid_write and owner.uid_write)):
        raise NotImplementedError(
            "binding mutation requires a write-capable invocation"
        )
    with closing(sqlite3.connect(model.path)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "CREATE TABLE IF NOT EXISTS bindings (user_id TEXT, bot_id TEXT, group_id TEXT, "
            "uid TEXT, sr_uid TEXT, bb_uid TEXT, bbb_uid TEXT, zzz_uid TEXT, wd_uid TEXT, "
            "PRIMARY KEY (user_id, bot_id))"
        )
        yield db


@contextmanager
def _uid_list(model, user_id, bot_id, game_name, *, new_group=None):
    column = model.get_gameid_name(game_name)
    with _transaction(model, user_id, bot_id, write=True, uid_write=True) as db:
        row = db.execute(
            f"SELECT {column} FROM bindings WHERE user_id=? AND bot_id=?",
            (user_id, bot_id),
        ).fetchone()
        if not _current.get().writable and new_group:
            raise NotImplementedError(
                "UID authority does not include a group association"
            )
        previous = (row[0] or "").split("_") if row and row[0] else []
        values = previous.copy()
        yield values
        if previous != values:
            db.execute(
                f"INSERT INTO bindings (user_id, bot_id, group_id, {column}) VALUES (?, ?, ?, ?) "
                f"ON CONFLICT(user_id,bot_id) DO UPDATE SET {column}=excluded.{column}",
                (user_id, bot_id, new_group, "_".join(values) or None),
            )


class GsBind:
    @staticmethod
    def get_gameid_name(game_name=None):
        column = "uid" if not game_name or game_name == "gs" else f"{game_name}_uid"
        if column not in _COLUMNS:
            raise ValueError("unknown game UID column")
        return column

    @classmethod
    async def select_data(cls, user_id, bot_id=None):
        with _transaction(cls, user_id, bot_id) as db:
            row = db.execute(
                "SELECT * FROM bindings WHERE user_id=? AND bot_id=?", (user_id, bot_id)
            ).fetchone()
            return SimpleNamespace(**dict(row)) if row else None

    @classmethod
    async def update_data(cls, user_id, bot_id, **data):
        if data.keys() != {"group_id"}:
            raise NotImplementedError(
                "only the group association can be updated directly"
            )
        owner = _current.get()
        if (
            owner
            and not owner.writable
            and owner.uid_write
            and data["group_id"] in (None, "")
        ):
            # The pinned get_uid refreshes an empty group on direct lookups.
            # A UID-only grant can acknowledge an existing, already-empty association.
            row = await cls.select_data(user_id, bot_id)
            if row is None:
                return -1
            if row.group_id in (None, ""):
                return 0
        with _transaction(cls, user_id, bot_id, write=True) as db:
            updated = db.execute(
                "UPDATE bindings SET group_id=? WHERE user_id=? AND bot_id=?",
                (data["group_id"], user_id, bot_id),
            )
            return 0 if updated.rowcount else -1

    @classmethod
    async def get_uid_list_by_game(cls, user_id, bot_id, game_name=None):
        row = await cls.select_data(user_id, bot_id)
        value = getattr(row, cls.get_gameid_name(game_name), None)
        if not value:
            return None
        return [uid for uid in value.split("_") if uid] or None

    @classmethod
    async def get_uid_by_game(cls, user_id, bot_id, game_name=None):
        values = await cls.get_uid_list_by_game(user_id, bot_id, game_name)
        return values[0] if values else None

    @classmethod
    async def insert_uid(
        cls,
        user_id,
        bot_id,
        uid,
        group_id=None,
        lenth_limit=None,
        is_digit=True,
        game_name=None,
    ):
        with _uid_list(cls, user_id, bot_id, game_name, new_group=group_id) as values:
            if lenth_limit and len(uid) != lenth_limit:
                return -1
            if not _current.get().writable and (
                type(uid) is not str
                or not uid.isascii()
                or not uid.isdigit()
                or len(uid) > 32
            ):
                return -3
            if is_digit and not uid.isdigit():
                return -3
            if not uid:
                return -1
            if uid in values:
                return -2
            values.append(uid)
        return 0

    @classmethod
    async def delete_uid(cls, user_id, bot_id, uid, game_name=None):
        with _uid_list(cls, user_id, bot_id, game_name) as values:
            if uid not in values:
                return -1
            values.remove(uid)
        return 0

    @classmethod
    async def switch_uid_by_game(cls, user_id, bot_id, uid=None, game_name=None):
        with _uid_list(cls, user_id, bot_id, game_name) as values:
            if not values:
                return -1
            if len(values) < 2:
                return -3
            if not uid:
                values.append(values.pop(0))
            elif uid not in values:
                return -2
            else:
                values.remove(uid)
                values.insert(0, uid)
        return 0


def install(package, data_root):
    class ProfileBindings(GsBind):
        path = Path(data_root) / "bindings.sqlite3"

    package("gsuid_core.utils.database")
    package("gsuid_core.utils.database.models").GsBind = ProfileBindings
    package("gsuid_core.utils.database.api").get_uid = get_uid
