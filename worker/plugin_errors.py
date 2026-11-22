"""Text error messages from GsCore commit 96cd7353be3100ff982bccc33e554961837c778e.

The native message table is distributed under GPL-3.0 with its license at
licenses/gsuid_core-COPYING.txt. The default command_start is empty.
"""

UID_HINT = "你还没有绑定过uid哦!\n请使用[绑定uid123456]命令绑定!"

MYS_HINT = "你还没有绑定过mysid哦!\n请使用[绑定mys1234]命令绑定!"

CK_HINT = """你还没有绑定过Cookie哦!
发送【ck帮助】获取帮助!
发送【扫码登陆】绑定CK&SK!
警告:绑定Cookie可能会带来未知的账号风险,请确保信任机器人管理员"""

_CHAR_HINT = (
    "请先使用【强制刷新】命令来缓存数据! \n或者使用【查询展柜角色】命令查看已缓存角色！"
)

CHAR_HINT = "你还没有{}的缓存噢！\n" + _CHAR_HINT

VERIFY_HINT = """出现验证码!请进行[绑定设备]以解除风险！"""

SK_HINT = "你还没有绑定过Stoken或者Stoken已失效~\n请加好友私聊Bot\n [扫码登陆] 或 [添加]后跟SK格式 以绑定SK"

UPDATE_HINT = """更新失败!更多错误信息请查看控制台...
>> 可以尝试使用
>> [gs强制更新](危险)
>> [gs强行强制更新](超级危险)!"""


ERROR_CODE = {
    -51: CK_HINT,
    -100: "您的cookie已经失效, 请重新获取!",
    -503: CK_HINT,
    10001: "您的cookie已经失效, 请重新获取!",
    10101: "当前查询CK已超过每日30次上限!",
    10102: "当前查询id已经设置了隐私, 无法查询!",
    1034: VERIFY_HINT,
    -10001: "请求体出错, 请检查具体实现代码...",
    -201: "你的账号可能已被封禁, 请联系米游社客服...",
    1008: "该API需要CK, 查询的用户/UID未绑定CK...",
    10104: "CK与用户信息不符, 请检查代码实现...",
    -999: VERIFY_HINT,
    -501002: "用户数据未公开, 请绑定/使用自己的CK！",
    10110: "米游社未查询到你的游戏角色, 请先前往米游社进行绑定游戏角色!",
    -199: "该活动不存在!",
}

error_dict = ERROR_CODE.copy()
error_dict.update(
    {
        -512009: "[留影叙佳期]已经获取过该内容~!",
        -501101: "当前角色冒险等阶未达到10级, 暂时无法参加此活动...",
        400: "[MINIGG]暂未找到此内容...",
        -400: "请输入更详细的名称...",
        125: "该充值方式暂时不可用!",
        126: "该充值方式不正确!",
    }
)


def get_error(retcode, message=None):
    if isinstance(message, dict):
        message = message.get("message", message.get("msg", "无可用信息。"))
    elif not isinstance(message, str):
        message = "无可用信息。"
    return error_dict.get(
        int(retcode), f"未知错误, 错误码为{retcode}!\n可能的错误消息: {message}"
    )


async def get_error_img(retcode, force_image=False, message=None):
    if force_image:
        raise NotImplementedError("native error artwork is unavailable")
    return get_error(retcode, message)
