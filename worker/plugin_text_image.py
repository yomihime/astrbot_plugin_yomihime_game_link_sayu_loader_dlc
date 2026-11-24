"""GsCore 0.12.2 drawing helpers, Genshin-bots/gsuid_core 96cd7353, GPL-3.0.

See licenses/gsuid_core-COPYING.txt.
"""

from __future__ import annotations

import math
from typing import Tuple, Union
from PIL import Image, ImageDraw, ImageFont
from plugin_help import core_font
from plugin_image_tools import _asset, _pixels


def get_div():
    return _asset("div.png")


def draw_center_text_by_line(
    img: ImageDraw.ImageDraw,
    pos: Tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: Union[Tuple[int, int, int, int], str],
    max_length: float,
    not_center: bool = False,
) -> float:
    pun = "。！？；!?…"
    gun = "。！？；!?」』"
    x, y = pos

    if hasattr(font, "getsize"):
        _, h = font.getsize("X")  # type: ignore
    else:
        bbox = font.getbbox("X")
        _, h = 0, bbox[3] - bbox[1]

    line = ""
    lenth = 0
    anchor = "la" if not_center else "mm"
    for index, char in enumerate(text):
        if hasattr(font, "getsize"):
            # 获取当前字符的宽度
            size, _ = font.getsize(char)  # type: ignore
        else:
            bbox = font.getbbox(char)
            size, _ = bbox[2] - bbox[0], bbox[3] - bbox[1]
        lenth += size
        line += char
        if lenth < max_length and char not in pun and char != "\n":
            pass
        else:
            if index + 1 < len(text) and text[index + 1] in gun:
                pass
            else:
                line = line.replace("\n", "")
                img.text((x, y), line, fill, font, anchor)
                line, lenth = "", 0
                y += h * 2.5
    else:
        img.text((x, y), line, fill, font, anchor)
    return y


async def str_lenth(r: str, size: int, limit: int = 540) -> str:
    result = ""
    temp = 0
    for i in r:
        if i == "\n":
            temp = 0
            result += i
            continue

        if temp >= limit:
            result += "\n" + i
            temp = 0
        else:
            result += i

        if i.isdigit():
            temp += round(size / 10 * 6)
        elif i == "/":
            temp += round(size / 10 * 2.2)
        elif i == ".":
            temp += round(size / 10 * 3)
        elif i == "%":
            temp += round(size / 10 * 9.4)
        else:
            temp += size
    return result


def get_str_size(r: str, font: ImageFont.FreeTypeFont, limit: int = 540) -> str:
    result = ""
    line = ""
    for i in r:
        if i == "\n":
            result += f"{line}\n"
            line = ""
            continue

        line += i

        if hasattr(font, "getsize"):
            size, _ = font.getsize(line)  # type: ignore
        else:
            bbox = font.getbbox(line)
            size, _ = bbox[2] - bbox[0], bbox[3] - bbox[1]

        if size >= limit:
            result += f"{line}\n"
            line = ""
    else:
        result += line
    return result


def get_height(content: str, size: int) -> int:
    line_count = content.count("\n")
    return (line_count + 1) * size


async def text2pic(text: str, max_size: int = 800, font_size: int = 24):
    if text.endswith("\n"):
        text = text[:-1]

    from sayu_compat import convert_img

    size = (max_size, len(text) * font_size // 3)
    if size[1]:
        _pixels(size)
    img = Image.new("RGB", size, (255, 255, 255))
    img_draw = ImageDraw.ImageDraw(img)
    y = draw_center_text_by_line(
        img_draw,
        (50, 50),
        text,
        core_font(font_size),
        "black",
        max_size - 80,
        True,
    )
    img = img.crop((0, 0, max_size, int(y + 80)))
    return await convert_img(img)


def number_to_chinese(num):
    units = [
        {"threshold": 10**8, "suffix": "亿"},  # 1e8 (100,000,000)
        {"threshold": 10**7, "suffix": "千万"},  # 1e7 (10,000,000)
        {"threshold": 10**4, "suffix": "万"},  # 1e4 (10,000)
        {"threshold": 10**3, "suffix": "千"},  # 1e3 (1,000)
    ]
    for unit in units:
        if num >= unit["threshold"]:
            value = num / unit["threshold"]
            truncated = math.floor(value * 10) / 10  # 截断一位小数，不四舍五入
            return f"{truncated:.1f}{unit['suffix']}"
    # 处理小于1e3的情况
    if isinstance(num, float) and num.is_integer():
        return str(int(num))
    else:
        return str(num)
