"""Profile-local SQLModel sessions. Owner data has a separate SQLite file per actor and adapter."""

import asyncio
from contextlib import asynccontextmanager, closing
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
import hashlib
import inspect
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from types import SimpleNamespace

from sqlalchemy import Index, event
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel import Field, SQLModel

from port_errors import Unsupported
import plugin_bindings
from plugin_orm_bind import BIND_ERROR, guard_bind, permits_bind, uid_columns
from plugin_orm_preferences import (
    OWNER_ERROR,
    guard_preferences,
    permits,
    preference_columns,
)
from service_settings import _grant

logger = SimpleNamespace(trace=logging.getLogger("sayu.orm").debug)
_runtime = None
_bound = ContextVar("plugin_orm_session", default=None)
_boot = ContextVar("plugin_orm_startup", default=None)
_COLUMN_OP = re.compile(
    r'ALTER\s+TABLE\s+"?(\w+)"?\s+(ADD|DROP)\s+COLUMN\s+"?(\w+)"?\b', re.I
)


@dataclass
class Access:
    root: object
    path: Path
    writable: bool
    startup: bool
    origin: object
    task: object
    preference: tuple[str, frozenset[str]] | None = None
    binding: tuple[str, frozenset[str]] | None = None

    def check(self):
        if self.root is not _runtime or asyncio.current_task() is not self.task:
            raise Unsupported(
                "database access is owned by a different task or generation"
            )
        if self.startup:
            if _boot.get() is not self.origin or not self.origin.writable:
                raise Unsupported("database startup grant expired")
        elif (
            plugin_bindings._current.get() is not self.origin or not self.origin.active
        ):
            raise Unsupported("database owner grant expired")


def _validate_values(owner, values):
    for key, expected in (("user_id", owner.actor), ("bot_id", owner.adapter)):
        if values.get(key) is not None and values[key] != expected:
            raise Unsupported("database operation is scoped to the current owner")


def session_decorator(function, read_only):
    signature = inspect.signature(function)

    @wraps(function)
    async def wrapper(self, *args, **kwargs):
        access = _runtime.access(read_only, self)
        if not access.startup:
            values = signature.bind(self, None, *args, **kwargs).arguments
            _validate_values(access.origin, values)
            for key, parameter in signature.parameters.items():
                if parameter.kind == parameter.VAR_KEYWORD:
                    _validate_values(access.origin, values.get(key, {}))
        bound = _bound.get()
        if bound is not None:
            session, previous = bound
            previous.check()
            if read_only:
                return await function(self, session, *args, **kwargs)
            async with session.begin_nested():
                return await function(self, session, *args, **kwargs)
        async with _runtime.connection_engine(access) as db:
            async with AsyncSession(
                db, expire_on_commit=False, close_resets_only=False
            ) as session:
                token = _bound.set((session, access))
                try:
                    if not access.startup:

                        @event.listens_for(session.sync_session, "before_flush")
                        def validate_rows(sync_session, flush_context, instances):
                            for row in sync_session.new.union(sync_session.dirty):
                                _validate_values(access.origin, vars(row))

                    result = await function(self, session, *args, **kwargs)
                    access.check()
                    await session.commit()
                    return result
                finally:
                    _bound.reset(token)

    return wrapper


def with_session(function):
    return session_decorator(function, False)


def with_read_session(function):
    return session_decorator(function, True)


_READ = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_TRANSACTION,
    sqlite3.SQLITE_SAVEPOINT,
    sqlite3.SQLITE_RECURSIVE,
}
_DML = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}
_SCHEMA = {
    sqlite3.SQLITE_ALTER_TABLE,
    sqlite3.SQLITE_CREATE_TABLE,
    sqlite3.SQLITE_CREATE_INDEX,
    sqlite3.SQLITE_DROP_TABLE,
    sqlite3.SQLITE_DROP_INDEX,
    sqlite3.SQLITE_REINDEX,
}
_INSPECTION = {
    "table_info",
    "table_xinfo",
    "index_list",
    "index_info",
    "index_xinfo",
    "foreign_key_list",
    "database_list",
    "read_uncommitted",
}


class PluginORM:
    def __init__(self, data, plugin):
        self.root = Path(data).resolve() / "orm"
        self.plugin = plugin
        self.migrations = []
        self.report = {}
        self.writer = asyncio.Lock()
        self.writer_task = None
        self.preferences = {}
        self.bindings = {}
        self.bind_tables = set()

    def path(self, name):
        path = self.root / name
        for candidate in (
            path,
            *(path.with_name(name + suffix) for suffix in ("-wal", "-shm", "-journal")),
        ):
            if candidate.resolve() != candidate or (
                candidate.exists()
                and (not candidate.is_file() or candidate.stat().st_nlink != 1)
            ):
                raise Unsupported(
                    "database must be a regular file in the private profile"
                )
        return path

    @asynccontextmanager
    async def startup(self):
        grant = _grant.get()
        if (
            not grant
            or not grant.writable
            or plugin_bindings._current.get() is not None
        ):
            raise Unsupported("ORM startup requires plugin lifecycle permissions")
        self.path("schema.sqlite3")
        self.root.mkdir(parents=True, exist_ok=True)
        boot = SimpleNamespace(writable=True)
        token = _boot.set(boot)
        try:
            yield self
            self.publish_empty_schema()
        finally:
            boot.writable = False
            _boot.reset(token)

    def access(self, read_only=False, model=None):
        task = asyncio.current_task()
        owner = plugin_bindings._current.get()
        if owner is None:
            grant = _boot.get()
            if grant is None or not grant.writable:
                raise Unsupported("database requires a private owner or plugin startup")
            return Access(
                self, self.path("schema.sqlite3"), not read_only, True, grant, task
            )
        if not owner.active or getattr(owner, "task", None) is not task:
            raise Unsupported("database requires the active private owner task")
        identity = json.dumps([owner.adapter, owner.actor], ensure_ascii=True).encode()
        path = self.path(hashlib.sha256(identity).hexdigest() + ".sqlite3")
        preference = None
        if (
            not owner.writable
            and owner.preferences
            and not read_only
            and isinstance(model, type)
        ):
            preference = self.preferences.get(model)
        binding = None
        if not owner.writable and isinstance(model, type) and model in self.bindings:
            binding = self.bindings[model]
            if not owner.uid_read or binding is None:
                raise Unsupported(BIND_ERROR)
        write = not read_only and (
            owner.writable
            or preference is not None
            or (binding is not None and owner.uid_write)
        )
        if not path.exists():
            template = self.path("empty.sqlite3")
            if write:
                self.clone_schema(template, path)
            else:
                path = template
        return Access(self, path, write, False, owner, task, preference, binding)

    def publish_empty_schema(self):
        # Plugin startup may seed records in its own store. A new actor inherits
        # table definitions, indexes and migration IDs only.
        original = self.path("schema.sqlite3")
        empty = self.path("empty.sqlite3")
        fd, name = tempfile.mkstemp(dir=self.root)
        os.close(fd)
        try:
            with closing(
                sqlite3.connect(original.as_uri() + "?mode=ro", uri=True)
            ) as src:
                with closing(sqlite3.connect(name)) as dst, dst:
                    schema = src.execute(
                        "SELECT sql FROM sqlite_master WHERE type IN ('table','index') "
                        "AND sql IS NOT NULL AND name NOT GLOB 'sqlite_*' ORDER BY type DESC, name"
                    )
                    for (ddl,) in schema:
                        dst.execute(ddl)
                    dst.executemany(
                        "INSERT INTO _sayu_migrations VALUES (?)",
                        src.execute("SELECT digest FROM _sayu_migrations"),
                    )
            os.replace(name, empty)
        finally:
            Path(name).unlink(missing_ok=True)

    @staticmethod
    def clone_schema(template, destination):
        # Only startup operates on the template. Never copy an actor's database.
        if not template.is_file():
            raise Unsupported("plugin database migration has not completed")
        fd, name = tempfile.mkstemp(dir=template.parent)
        os.close(fd)
        try:
            with closing(
                sqlite3.connect(template.as_uri() + "?mode=ro", uri=True)
            ) as source:
                with closing(sqlite3.connect(name)) as target:
                    source.backup(target)
            os.replace(name, destination)
        finally:
            Path(name).unlink(missing_ok=True)

    @asynccontextmanager
    async def connection_engine(self, access):
        if not access.writable:
            async with self.open_engine(access) as db:
                yield db
            return
        if asyncio.current_task() is self.writer_task:
            raise Unsupported("nested writes must use the current ORM session")
        async with self.writer:
            self.writer_task = asyncio.current_task()
            try:
                async with self.open_engine(access) as db:
                    yield db
            finally:
                self.writer_task = None

    @asynccontextmanager
    async def open_engine(self, access):
        access.check()
        # New connection per checkout, explicit SQLite transactions including DDL.
        # Read-only connections also use SQLite's URI-level open restriction.
        uri = access.path.as_uri() + ("?mode=rwc" if access.writable else "?mode=ro")
        db = create_async_engine(
            "sqlite+aiosqlite:///" + uri + "&uri=true",
            poolclass=NullPool,
            hide_parameters=True,
            connect_args={"cached_statements": 0},
        )
        rejected = []

        def authorize(action, first, second, database, trigger):
            live = access.origin.writable if access.startup else access.origin.active
            if not live and not (
                action == sqlite3.SQLITE_TRANSACTION and first == "ROLLBACK"
            ):
                rejected.append(action)
                return sqlite3.SQLITE_DENY
            allowed = action in _READ
            if action == sqlite3.SQLITE_FUNCTION:
                allowed = (second or "").lower() not in {
                    "load_extension",
                    "readfile",
                    "writefile",
                    "fts3_tokenizer",
                }
            elif action == sqlite3.SQLITE_PRAGMA:
                allowed = (first or "").lower() in _INSPECTION
            elif action in _DML:
                allowed = access.writable and (
                    access.startup or first not in {"_sayu_migrations", "sqlite_master"}
                )
            elif action in _SCHEMA:
                allowed = access.startup and access.writable
            if access.preference and (action in _DML or action == sqlite3.SQLITE_READ):
                allowed = permits(
                    access.preference, action, first, second, database, trigger
                )
            if action in _DML or action == sqlite3.SQLITE_READ:
                if access.binding:
                    allowed = permits_bind(
                        access, action, first, second, database, trigger
                    )
                elif (
                    not access.startup
                    and not access.origin.writable
                    and (first or "").casefold() in self.bind_tables
                ):
                    allowed = False
            if not allowed:
                rejected.append(action)
            return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY

        @event.listens_for(db.sync_engine, "connect")
        def configure(connection, record):
            connection.isolation_level = None
            if access.preference:
                connection.run_async(lambda driver: guard_preferences(driver, access))
            if access.binding:
                connection.run_async(lambda driver: guard_bind(driver, access))
            connection.run_async(lambda driver: driver.set_authorizer(authorize))

        @event.listens_for(db.sync_engine, "begin")
        def begin(conn):
            conn.exec_driver_sql("BEGIN")

        @event.listens_for(db.sync_engine, "before_cursor_execute")
        def current(conn, cursor, statement, parameters, context, executemany):
            access.check()

        @event.listens_for(db.sync_engine, "commit")
        def before_commit(conn):
            access.check()

        try:
            yield db
        except DBAPIError as exc:
            if access.binding and str(exc.orig) == BIND_ERROR:
                raise Unsupported(BIND_ERROR) from exc
            if access.preference and str(exc.orig) == OWNER_ERROR:
                raise Unsupported(OWNER_ERROR) from exc
            if rejected:
                raise Unsupported(
                    "SQL operation outside the plugin database grant"
                ) from exc
            raise
        finally:
            await db.dispose()

    @asynccontextmanager
    async def connect(self):
        # Direct plugin-engine calls are needed by startup schema inspections.
        access = self.access(True)
        async with self.connection_engine(access) as db, db.connect() as conn:
            yield conn

    @asynccontextmanager
    async def begin(self):
        access = self.access()
        async with self.connection_engine(access) as db, db.begin() as conn:
            yield conn

    def tables(self):
        prefix = (self.plugin + ".", "plugins." + self.plugin + ".")
        models = sys.modules["gsuid_core.utils.database.models"]
        base_id = sys.modules["gsuid_core.utils.database.base_models"].BaseIDModel
        bind = sys.modules["gsuid_core.utils.database.base_models"].Bind
        tables = {models.Subscribe.__table__, models.CoreUser.__table__}
        todo = list(SQLModel.__subclasses__())
        seen = set()
        while todo:
            cls = todo.pop()
            if cls in seen:
                continue
            seen.add(cls)
            todo.extend(cls.__subclasses__())
            table = getattr(cls, "__table__", None)
            if table is not None and (
                cls.__module__.startswith(prefix)
                or cls.__module__ in (self.plugin, "plugins." + self.plugin)
            ):
                tables.add(table)
                if issubclass(cls, bind):
                    self.bindings[cls] = uid_columns(table)
                    self.bind_tables.add(table.name.casefold())
                columns = preference_columns(cls, table, base_id)
                if columns:
                    self.preferences[cls] = (table.name, columns)
        return sorted(tables, key=lambda t: t.name)

    async def migrate(self):
        access = self.access()
        if not access.startup:
            raise Unsupported("schema migration requires plugin startup")
        self.report = {
            "files": 0,
            "applied": 0,
            "already_applied": 0,
            "schema_present": 0,
            "failed": 0,
        }
        paths = [access.path, *sorted(self.root.glob("[0-9a-f]" * 64 + ".sqlite3"))]
        for path in paths:
            access.path = self.path(path.name)
            counts = dict.fromkeys(("applied", "already_applied", "schema_present"), 0)
            try:
                async with self.connection_engine(access) as db, db.begin() as conn:
                    await conn.run_sync(
                        lambda sync: SQLModel.metadata.create_all(
                            sync, tables=self.tables()
                        )
                    )
                    await conn.exec_driver_sql(
                        "CREATE TABLE IF NOT EXISTS _sayu_migrations (digest TEXT PRIMARY KEY)"
                    )
                    ledger = set(
                        (
                            await conn.exec_driver_sql(
                                "SELECT digest FROM _sayu_migrations"
                            )
                        ).scalars()
                    )
                    for statement in self.migrations:
                        digest = hashlib.sha256(statement.encode()).hexdigest()
                        if digest in ledger:
                            counts["already_applied"] += 1
                            continue
                        match = _COLUMN_OP.match(statement.strip())
                        already = False
                        if match:
                            name, operation, column = match.groups()
                            info = await conn.exec_driver_sql(
                                f'PRAGMA table_info("{name}")'
                            )
                            cols = {r[1].lower() for r in info}
                            already = bool(cols) and (
                                (column.lower() in cols) == (operation.upper() == "ADD")
                            )
                        if not already:
                            await conn.exec_driver_sql(statement)
                        counts["schema_present" if already else "applied"] += 1
                        await conn.exec_driver_sql(
                            "INSERT INTO _sayu_migrations VALUES (?)", (digest,)
                        )
                        ledger.add(digest)
            except BaseException:
                self.report["failed"] += 1
                raise
            for key, count in counts.items():
                self.report[key] += count
            self.report["files"] += 1
        return dict(self.report)


def install_orm(package, data_root, plugin):
    global _runtime
    import plugin_orm_models as native
    from plugin_core_user import CoreUser

    class Subscribe(native.BaseModel, table=True):
        __table_args__ = (
            Index("ix_subscribe_task_name_uid", "task_name", "uid"),
            {"extend_existing": True},
        )
        WS_BOT_ID: str | None = Field(default=None)
        group_id: str | None = Field(default=None, index=True)
        task_name: str = Field(default=None, index=True)
        bot_self_id: str = Field(default=None)
        user_type: str = Field(default=None)
        extra_message: str | None = Field(default=None)
        uid: str | None = Field(default=None, index=True)
        extra_data: str | None = Field(default=None)
        msg_id: str | None = Field(default=None)

        @classmethod
        async def ensure_owner(cls, *args, **kwargs):
            raise Unsupported(
                "subscription owner provisioning requires a separate grant"
            )

        async def send(self, *args, **kwargs):
            raise Unsupported("subscription delivery requires a target transport")

    _runtime = PluginORM(data_root, plugin)
    base = package("gsuid_core.utils.database.base_models")
    for name in ("BaseIDModel", "BaseBotIDModel", "BaseModel", "Bind", "User"):
        setattr(base, name, getattr(native, name))
    base.with_session = with_session
    base.with_read_session = with_read_session
    base.engine = SimpleNamespace(begin=_runtime.begin, connect=_runtime.connect)
    package("gsuid_core.utils.database.startup").exec_list = _runtime.migrations
    models = sys.modules["gsuid_core.utils.database.models"]
    models.Subscribe = Subscribe
    models.CoreUser = CoreUser
    return _runtime
