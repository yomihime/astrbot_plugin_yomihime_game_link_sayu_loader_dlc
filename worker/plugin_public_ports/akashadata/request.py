"""Parse Akasha's three public JavaScript datasets with GsCore's field semantics."""

import json

from plugin_public_api import HEADER, public_get
from .api import AKASHA_ABYSS_URL, AKASHA_CHAR_URL, AKASHA_RANK_URL
from .models import AkashaAbyssData, AKaShaCharData, AKaShaRank


async def _akasha_request(url, method="GET", header=HEADER, params=None, data=None):
    if (
        url not in (AKASHA_ABYSS_URL, AKASHA_CHAR_URL, AKASHA_RANK_URL)
        or method != "GET"
        or header != HEADER
        or data
    ):
        raise NotImplementedError("unsupported Akasha request")
    return (await public_get(url, params=params)).text


async def get_akasha_abyss_info() -> AkashaAbyssData:
    text = await _akasha_request(AKASHA_ABYSS_URL)
    return json.loads(text.lstrip("var static_abyss_total ="))


async def get_akasha_all_char_info() -> dict[str, AKaShaCharData]:
    text = await _akasha_request(AKASHA_CHAR_URL)
    payload = text.replace("\\", "").lstrip("var static_card_details =")
    for escaped, literal in (('"[', "["), (']"', "]"), ('"{', "{"), ('}"', "}")):
        payload = payload.replace(escaped, literal)
    return json.loads(payload)


async def get_akasha_abyss_rank(is_info=False) -> AKaShaRank:
    text = await _akasha_request(AKASHA_RANK_URL)
    blocks = text.lstrip("var static_abyss_total =").split(";")
    version = json.loads(blocks[0].lstrip("var static_schedule_version_dict ="))
    ranks = json.loads(blocks[1].lstrip("var static_abyss_record_dict ="))
    return version if is_info else ranks
