"""Owner-private access to the fixed GsCore credential cache."""

from sqlalchemy import select

from port_errors import Unsupported


_ROLES = {"uid", "sr_uid", "mys_id"}
_METHODS = {"select_cache_cookie", "refresh_cache", "insert_cache_data"}


def check_cache_call(model, function):
    from plugin_account_models import GsCache
    from plugin_orm_models import Cache

    name = getattr(function, "__name__", None)
    if (
        model is not GsCache
        or name not in _METHODS
        or getattr(Cache, name).__func__.__wrapped__ is not function
    ):
        raise Unsupported("credential cache operation requires a separate grant")


async def verify_cookie(access, session, cookie, roles):
    from plugin_account_models import GsUser

    if (
        type(cookie) is not str
        or not cookie
        or not roles
        or not roles.keys() <= _ROLES
        or any(type(v) not in (str, int) or not str(v) for v in roles.values())
    ):
        raise Unsupported("credential cache requires an existing owner account")
    query = (
        select(GsUser.id)
        .where(
            GsUser.user_id.collate("BINARY") == access.origin.actor,
            GsUser.bot_id.collate("BINARY") == access.origin.adapter,
            GsUser.cookie.collate("BINARY") == cookie,
            *(
                getattr(GsUser, key).collate("BINARY") == value
                for key, value in roles.items()
            ),
        )
        .limit(1)
    )
    # Only this fixed, parameterized ownership check can read GsUser in the
    # same SQLite transaction. The native cache method still sees one table.
    access.checking_cache_owner = True
    try:
        found = (await session.execute(query)).scalar_one_or_none()
    finally:
        access.checking_cache_owner = False
    if found is None:
        raise Unsupported("credential cache requires an existing owner account")


async def call_cache(access, function, model, session, values, args, kwargs):
    if function.__name__ == "insert_cache_data":
        await verify_cookie(access, session, values["cookie"], values.get("data", {}))
    elif (
        type(values["uid"]) not in (str, int)
        or not str(values["uid"])
        or (
            values.get("game_name") is not None and type(values["game_name"]) is not str
        )
    ):
        raise Unsupported("credential cache lookup requires a scalar role identifier")
    result = await function(model, session, *args, **kwargs)
    if function.__name__ == "select_cache_cookie" and result is not None:
        column = model.get_gameid_name(values["game_name"])
        await verify_cookie(access, session, result, {column: values["uid"]})
    return result
