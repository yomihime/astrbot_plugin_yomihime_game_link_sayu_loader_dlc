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


async def draw_add_ck_pic(msg):
    from PIL import ImageDraw
    from gsuid_core.utils.fonts.fonts import core_font
    from gsuid_core.utils.image.image_tools import get_status_icon, get_v4_bg
    from plugin_image_tools import _asset

    card = get_v4_bg(800, 260, is_blur=True)
    with _asset("ck_fg.png") as overlay:
        card.paste(overlay, (0, 0), overlay)
    count = msg.count("成功")
    with get_status_icon(count) as icon:
        card.paste(icon, (47, 33), icon)
    if count == 0:
        title = "绑定账号失败..."
    elif count == 1:
        title = "绑定Cookies成功！Stoken失败..."
    else:
        title = "绑定Cookie、Stoken成功！"
    first, *rest = msg.split("\n", 2)
    second = rest[0] if rest else "可以尝试退出米游社登陆重新登陆获取!"
    if len(first) >= 27:
        first = first[:27] + "..."
    if len(second) >= 27:
        second = second[:27] + "..."
    if count:
        first += "可使用[绑定信息]查看账号状态！"
        second = "> 我们仍然推荐你进行 [绑定设备]"
    draw = ImageDraw.Draw(card)
    draw.text((108, 58), title, (29, 29, 29), core_font(40), "lm")
    draw.text((58, 122), f"> {first}", (72, 72, 72), core_font(32), "lm")
    draw.text((58, 174), f"> {second}", (72, 72, 72), core_font(32), "lm")
    return card, count


async def _deal_ck_to_pic(im):
    from gsuid_core.utils.image.convert import convert_img

    card, count = await draw_add_ck_pic(im)
    return await convert_img(card), count


async def deal_ck(bot_id, mes, user_id, mode="PIC"):
    """Perform native local validation in the current owner's account invocation."""
    from http.cookies import CookieError
    from plugin_account_intake import require_account_owner
    from port_errors import Unsupported
    from ._native_input import _deal_ck, PUBLIC_ERRORS

    require_account_owner(bot_id, user_id)
    try:
        message = await _deal_ck(bot_id, mes, user_id)
    except CookieError:
        # SimpleCookie can embed arbitrary caller input in its exception text.
        raise Unsupported("invalid account input") from None
    if message not in PUBLIC_ERRORS:
        raise Unsupported("credential-bearing status requires a trusted result projection")
    picture, count = await _deal_ck_to_pic(message)
    return (picture if mode == "PIC" else message), count


async def get_ck_by_stoken(bot_id, user_id):
    from plugin_account_intake import require_account_owner
    from . import _native_owner_refresh

    require_account_owner(bot_id, user_id)
    return await _native_owner_refresh.get_ck_by_stoken(bot_id, user_id)


async def refresh_ck_by_uid_list(bot_id, uid_dict, game_name=None):
    from http.cookies import CookieError
    from gsuid_core.utils.database.models import GsBind
    from plugin_account_intake import require_account_owner
    from plugin_bindings import _current
    from port_errors import Unsupported
    from . import _native_refresh

    owner = _current.get()
    require_account_owner(bot_id, owner.actor if owner else None)
    # Snapshot before the first await. A batch with any foreign target must not
    # process an earlier valid entry.
    if type(uid_dict) is not dict:
        raise Unsupported("refresh requires an explicit UID mapping")
    targets = dict(uid_dict)
    if any(
        type(uid) is not str
        or not uid.isascii()
        or not uid.isdigit()
        or type(actor) is not str
        or actor != owner.actor
        for uid, actor in targets.items()
    ):
        raise Unsupported("refresh targets must belong to the current account owner")
    if targets:
        bound = await GsBind.get_uid_list_by_game(owner.actor, bot_id, game_name)
        if not targets.keys() <= set(bound or ()):
            raise Unsupported("refresh targets must be bound to the current account owner")
    try:
        return await _native_refresh.refresh_ck_by_uid_list(bot_id, targets, game_name)
    except CookieError:
        raise Unsupported("invalid stored account input") from None
