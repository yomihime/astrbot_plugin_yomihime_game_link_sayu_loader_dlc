"""Identify simple plugin-owned presentation settings and constrain their SQLite connection."""

import re
import sqlite3

from sqlalchemy import Integer, String
from sqlmodel.sql.sqltypes import AutoString

from port_errors import Unsupported


_LOCALE_COLUMNS = {"lang", "language", "locale"}
OWNER_ERROR = "preference row must belong to the current owner"


def preference_columns(model, table, base_id):
    # Require a plugin's own one-row-per-user model. Models with account,
    # routing or unknown fields need a separate grant.
    if (
        model.__bases__ != (base_id,)
        or table.schema is not None
        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", table.name)
        or table.foreign_keys
    ):
        return None
    columns = table.c
    fields = frozenset(columns.keys())
    values = fields - {"id", "user_id"}
    if not (
        {"id", "user_id"} <= fields
        and values
        and values <= _LOCALE_COLUMNS
        and tuple(table.primary_key.columns.keys()) == ("id",)
        and isinstance(columns.id.type, Integer)
        and columns.user_id.unique
        and all(
            isinstance(columns[k].type, (String, AutoString)) for k in fields - {"id"}
        )
    ):
        return None
    return fields


async def guard_physical_table(driver, table, columns):
    # Check the physical database as well as the imported model. Older profiles
    # may carry extra columns or triggers that the current class never declared.
    async with driver.execute(f'PRAGMA main.table_xinfo("{table}")') as cursor:
        rows = await cursor.fetchall()
    if (
        {row[1] for row in rows} != columns
        or any(row[6] for row in rows)
        or [(r[1], r[2].upper()) for r in rows if r[5]] != [("id", "INTEGER")]
        or any(
            not re.fullmatch(r"(?:TEXT|VARCHAR(?:\(\d+\))?)", r[2].upper())
            for r in rows
            if r[1] != "id"
        )
    ):
        raise Unsupported("owner table differs from its declared model")
    async with driver.execute(
        "SELECT type FROM main.sqlite_master WHERE tbl_name=? COLLATE NOCASE "
        "AND type IN ('table', 'view', 'trigger')",
        (table,),
    ) as cursor:
        if await cursor.fetchall() != [("table",)]:
            raise Unsupported("owner table has additional database behavior")
    async with driver.execute(f'PRAGMA main.foreign_key_list("{table}")') as cursor:
        if await cursor.fetchone():
            raise Unsupported("owner table references another model")


async def guard_preferences(driver, access):
    table, columns = access.preference
    await guard_physical_table(driver, table, columns)
    async with driver.execute(
        "SELECT 1 FROM pragma_index_list(?) AS idx WHERE idx.[unique] AND NOT idx.partial "
        "AND (SELECT count(*)=1 AND min(name)='user_id' FROM pragma_index_info(idx.name))",
        (table,),
    ) as cursor:
        if not await cursor.fetchone():
            raise Unsupported("preference table requires a unique user identity")
    async with driver.execute(
        f'SELECT 1 FROM main."{table}" WHERE user_id IS NOT ? LIMIT 1',
        (access.origin.actor,),
    ) as cursor:
        if await cursor.fetchone():
            raise Unsupported(OWNER_ERROR)
    await driver.create_function("_sayu_owner", 0, lambda: access.origin.actor)
    for operation in ("INSERT", "UPDATE"):
        async with driver.execute(
            f"CREATE TEMP TRIGGER _sayu_owner_{operation} BEFORE {operation} "
            f'ON main."{table}" WHEN NEW.user_id IS NOT _sayu_owner() '
            f"BEGIN SELECT RAISE(ABORT, '{OWNER_ERROR}'); END"
        ):
            pass


def permits(preference, action, table, column, database, trigger):
    name, fields = preference
    if database != "main" or table != name:
        return False
    if action == sqlite3.SQLITE_READ:
        return column in fields or column == ""
    if trigger:
        return False
    if action == sqlite3.SQLITE_UPDATE:
        return column in fields - {"id", "user_id"}
    return action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_DELETE)
