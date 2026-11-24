from bs4 import BeautifulSoup, Tag
from plugin_public_api import public_get


HHW_ABYSS = "https://genshin.honeyhunterworld.com/d_1001/?lang=CHS"


async def get_abyss_review_raw():
    return (await public_get(HHW_ABYSS)).content


async def get_abyss_review(raw_data, _id, floor):
    soup = BeautifulSoup(raw_data, "lxml")
    section = soup.find("section", id=f"Variant #{_id}")
    if not isinstance(section, Tag):
        return None
    cells = section.find_all("td")
    halves = []
    for idx, cell in enumerate(cells):
        if "Monsters" in cell.text:
            names = [link.text for link in cells[idx + 1].find_all("a") if link.text]
            if names:
                halves.append(names)
    suffixes = ("-1上", "-1下", "-2上", "-2下", "-3上", "-3下")
    return {f"{floor}{suffix}": halves[index] for index, suffix in enumerate(suffixes)}
