"""Access to a single mapped account table in the caller's private database."""

import re
import sqlite3

from sqlalchemy.dialects.sqlite import dialect

from plugin_orm_preferences import guard_physical_table
from port_errors import Unsupported


ACCOUNT_ERROR = "account records require the matching private owner"


def account_columns(table):
    fields = frozenset(table.c.keys())
    if (
        table.schema is not None
        or table.foreign_keys
        or not all(
            re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", n) for n in (table.name, *fields)
        )
        or tuple(table.primary_key.columns.keys()) != ("id",)
    ):
        return None
    sqlite = dialect()
    return table.name, {
        col.name: col.type.compile(dialect=sqlite).upper() for col in table.c
    }


async def guard_account(driver, access):
    table, columns = access.account
    await guard_physical_table(driver, table, columns.keys(), column_types=columns)
    keys = [("user_id", access.origin.actor), ("bot_id", access.origin.adapter)]
    keys = [(key, value) for key, value in keys if key in columns]
    if not keys:
        # Alias tables have no sender fields. Their file is allocated per actor
        # and adapter by PluginORM.access().
        return
    mismatch = " OR ".join(f'"{key}" IS NOT ?' for key, _ in keys)
    async with driver.execute(
        f'SELECT 1 FROM main."{table}" WHERE {mismatch} LIMIT 1',
        tuple(value for _, value in keys),
    ) as cursor:
        if await cursor.fetchone():
            raise Unsupported(ACCOUNT_ERROR)
    if access.writable:
        for key, value in keys:
            await driver.create_function(f"_sayu_{key}", 0, lambda v=value: v)
        invalid = " OR ".join(f'NEW."{key}" IS NOT _sayu_{key}()' for key, _ in keys)
        for op in ("INSERT", "UPDATE"):
            async with driver.execute(
                f'CREATE TEMP TRIGGER _sayu_account_{op} BEFORE {op} ON main."{table}" '
                f"WHEN {invalid} BEGIN SELECT RAISE(ABORT, '{ACCOUNT_ERROR}'); END"
            ):
                pass


def permits_account(access, action, table, column, database, trigger):
    name, fields = access.account
    if database != "main" or (table or "").casefold() != name.casefold():
        return False
    if action == sqlite3.SQLITE_READ:
        return not column or column in fields
    if not access.writable or trigger:
        return False
    if action == sqlite3.SQLITE_UPDATE:
        return column != "id" and column in fields
    return action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_DELETE)
