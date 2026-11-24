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
