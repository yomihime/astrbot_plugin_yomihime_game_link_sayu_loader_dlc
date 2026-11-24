"""Ambr public game data, read through the profile's bounded HTTP and cache ports."""

from io import BytesIO
from pathlib import Path, PureWindowsPath
import re
import tempfile

from PIL import Image
from plugin_public_api import HEADER, MAX_BODY, cache_data, public_get
from . import api


def _part(value):
    text = str(value)
    if (
        not text
        or text in (".", "..")
        or re.search(r'[<>:"/\\|?*\x00-\x1f]', text)
        or PureWindowsPath(text).is_reserved()
    ):
        raise ValueError("invalid Ambr identifier")
    return text


async def _ambr_request(url, method="GET", header=HEADER, params=None, data=None):
    if (
        not url.startswith(api.AMBR_BASE_URL + "/")
        or method != "GET"
        or header != HEADER
        or data
    ):
        raise NotImplementedError("unsupported Ambr request")
    result = (await public_get(url, params=params)).json()
    if result and "code" in result:
        result["response"] = result["code"]
    return result


async def _listed(url):
    value = await _ambr_request(url)
    return value["data"]["items"] if isinstance(value, dict) else None


async def _data(url):
    value = await _ambr_request(url)
    return (
        value["data"] if isinstance(value, dict) and value["response"] == 200 else None
    )


async def get_ambr_char_list():
    return await _listed(api.AMBR_CHAR_LIST_URL)


async def get_ambr_weapon_list():
    return await _listed(api.AMBR_WEAPON_LIST_URL)


async def get_ambr_reliquary_list():
    return await _listed(api.AMBR_RELIQUARY_URL)


async def get_ambr_event_info():
    value = await _ambr_request(api.AMBR_EVENT_URL)
    return value if isinstance(value, dict) else None


@cache_data
async def get_ambr_reliquary_data(_id, cache_path=None):
    return await _data(f"{api.AMBR_RELIQUARY_URL}/{_part(_id)}")


@cache_data
async def get_ambr_char_data(id, cache_path=None):
    return await _data(api.AMBR_CHAR_URL.format(_part(id)))


@cache_data
async def get_ambr_monster_data(id, cache_path=None):
    return await _data(api.AMBR_MONSTER_URL.format(_part(id)))


@cache_data
async def get_ambr_gcg_detail(id, cache_path=None):
    return await _data(api.AMBR_GCG_DETAIL.format(_part(id)))


@cache_data
async def get_ambr_weapon_data(id, cache_path=None):
    return await _data(api.AMBR_WEAPON_URL.format(_part(id)))


async def get_ambr_gcg_list():
    return await _data(api.AMBR_GCG_LIST_URL)


async def get_ambr_monster_list():
    return await _data(api.AMBR_MONSTER_LIST)


async def get_ambr_daily_data():
    days = await _data(api.AMBR_DAILY_URL)
    return (
        {day: list(entries.values()) for day, entries in days.items()}
        if days is not None
        else None
    )


async def get_all_upgrade():
    return await _data(api.AMBR_UPGRADE_URL)


async def get_all_book_id():
    books = await _data(api.AMBR_BOOK_URL)
    return books["items"] if books is not None else None


async def get_book_volume(id):
    return await _data(api.AMBR_BOOK_DETAILS_URL.format(_part(id)))


async def get_story_data(story_id):
    return await _data(api.AMBR_BOOK_DATA_URL.format(_part(story_id)))


def _image(raw):
    image = Image.open(BytesIO(raw))
    if image.width * image.height > 16_000_000:
        image.close()
        raise ValueError("Ambr image exceeds pixel limit")
    image.load()
    return image


async def get_ambr_icon(type, icon_name, path, ui_name=None, save_name=None):
    from gsuid_core.data_store import get_res_path

    name = f"{_part(icon_name)}.png"
    if ui_name:
        name = f"UI_{_part(ui_name)}_{name}"
        url = f"{api.AMBR_ICON_URL}/{name}"
    else:
        url = f"{api.AMBR_ICON_URL}/{_part(type)}/{name}"
    destination = Path(path) / (f"{_part(save_name)}.png" if save_name else name)
    if (
        not destination.resolve().is_relative_to(get_res_path())
        or destination.is_symlink()
    ):
        raise ValueError("Ambr image outside profile resources")
    if destination.exists():
        with destination.open("rb") as stream:
            raw = stream.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            raise ValueError("Ambr image exceeds size limit")
        return _image(raw)
    response = await public_get(url)
    if response.status_code != 200:
        return Image.new("RGBA", (256, 256), (0, 0, 0))
    image = _image(response.content)
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(response.content)
            stream.close()
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    return image
