"""Account input and bound-role lookup for the public cookie manager API.

Field names and precedence follow gsuid_core 96cd7353, GPL-3.0.
See licenses/gsuid_core-COPYING.txt.
"""

id_list = [
    "login_uid",
    "login_uid_v2",
    "account_id",
    "account_id_v2",
    "stuid",
    "stuid_v2",
    "ltuid",
    "ltuid_v2",
]
sk_list = ["stoken", "stoken_v2"]
ck_list = ["cookie_token", "cookie_token_v2"]
lt_list = ["login_ticket", "login_ticket_v2"]
mid_list = [
    "mid",
    "account_mid",
    "account_mid_v2",
    "ltmid",
    "ltmid_v2",
    "stmid",
    "stmid_v2",
]
persist_cookie_keys = {
    *id_list,
    *sk_list,
    *ck_list,
    *mid_list,
    "ltoken",
    "ltoken_v2",
}


async def get_account_id(simp_dict):
    return get_cookie_value(simp_dict, id_list)


def get_cookie_value(simp_dict, keys):
    for key in keys:
        if key in simp_dict:
            return simp_dict[key].value
    return ""


def serialize_cookie(cookie_data):
    return ";".join(f"{key}={value}" for key, value in cookie_data.items() if value)


def get_persist_cookie_data(simp_dict):
    return {
        key: morsel.value
        for key, morsel in simp_dict.items()
        if key in persist_cookie_keys and morsel.value
    }


def is_overseas_account(uid, sr_uid, zzz_uid):
    if uid:
        return int(uid[0]) >= 6
    if sr_uid:
        return int(sr_uid[0]) >= 6
    return bool(zzz_uid and len(zzz_uid) >= 10)


async def get_all_bind_uid(bot_id, user_id):
    from gsuid_core.utils.database.models import GsBind

    roles = []
    for game in (None, "sr", "zzz", "bbb"):
        roles.append(await GsBind.get_uid_by_game(user_id, bot_id, game))
    return tuple(roles)
