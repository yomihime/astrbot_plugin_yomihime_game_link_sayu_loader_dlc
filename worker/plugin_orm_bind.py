"""Numeric UID operations on plugin-owned SQLModel Bind tables."""

import re
import sqlite3

from sqlalchemy import Integer, String
from sqlmodel.sql.sqltypes import AutoString

from plugin_orm_preferences import guard_physical_table
from port_errors import Unsupported


_IDENTITY = {"id", "user_id", "bot_id", "group_id"}
BIND_ERROR = "operation exceeds the numeric UID grant"


def uid_columns(table):
    fields = frozenset(table.c.keys())
    values = fields - _IDENTITY
    if (
        not _IDENTITY <= fields
        or not values
        or table.schema is not None
        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", table.name)
        or table.foreign_keys
        or tuple(table.primary_key.columns.keys()) != ("id",)
        or not isinstance(table.c.id.type, Integer)
        or any(not re.fullmatch(r"(?:[a-z][a-z0-9_]*_)?uid", key) for key in values)
        or any(
            not isinstance(table.c[key].type, (String, AutoString))
            for key in fields - {"id"}
        )
    ):
        return None
    return table.name, fields


def _valid_list(value):
    if value is None or value == "":
        return True
    return (
        isinstance(value, str)
        and value.isascii()
        and all(token.isdigit() and len(token) <= 32 for token in value.split("_"))
    )


async def guard_bind(driver, access):
    table, fields = access.binding
    await guard_physical_table(driver, table, fields)
    await driver.create_function("_sayu_uid_list", 1, _valid_list)
    uids = sorted(fields - _IDENTITY)
    bad_values = " OR ".join(f'NOT _sayu_uid_list("{key}")' for key in uids)
    async with driver.execute(
        f'SELECT 1 FROM main."{table}" WHERE user_id IS NOT ? OR bot_id IS NOT ? '
        f"OR {bad_values} LIMIT 1",
        (access.origin.actor, access.origin.adapter),
    ) as cursor:
        if await cursor.fetchone():
            raise Unsupported(BIND_ERROR)
    if not access.writable:
        return
    await driver.create_function("_sayu_actor", 0, lambda: access.origin.actor)
    await driver.create_function("_sayu_adapter", 0, lambda: access.origin.adapter)
    value_checks = " OR ".join(f'NOT _sayu_uid_list(NEW."{key}")' for key in uids)
    invalid = (
        "NEW.user_id IS NOT _sayu_actor() OR NEW.bot_id IS NOT _sayu_adapter() OR "
        + value_checks
    )
    empty = " AND ".join(f"coalesce(NEW.\"{key}\",'')=''" for key in uids)
    for operation, extra in (
        (
            "INSERT",
            f"coalesce(NEW.group_id,'') <> '' OR ({empty}) OR EXISTS (SELECT 1 FROM main.\"{table}\")",
        ),
        ("UPDATE", "coalesce(NEW.group_id,'') IS NOT coalesce(OLD.group_id,'')"),
    ):
        # The native Bind base stores an ordered list in each UID column. The
        # delete operation clears that list and leaves the row and its group alone.
        async with driver.execute(
            f'CREATE TEMP TRIGGER _sayu_bind_{operation} BEFORE {operation} ON main."{table}" '
            f"WHEN {invalid} OR {extra} BEGIN SELECT RAISE(ABORT, '{BIND_ERROR}'); END"
        ):
            pass


def permits_bind(access, action, table, column, database, trigger):
    name, fields = access.binding
    if database != "main" or name.casefold() != (table or "").casefold():
        return False
    if action == sqlite3.SQLITE_READ:
        return column == "" or column in fields
    if not access.writable or trigger:
        return False
    if action == sqlite3.SQLITE_UPDATE:
        return column in fields - {"id", "user_id", "bot_id"}
    return action == sqlite3.SQLITE_INSERT
