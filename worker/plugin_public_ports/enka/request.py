from plugin_public_api import public_get
from .models import EnkaData


ADDRESS = {"enka": "https://enka.network", "microgg": "http://profile.microgg.cn"}


async def get_enka_info(uid: str, address="enka") -> EnkaData:
    origin = ADDRESS[address]
    if not str(uid).isascii() or not str(uid).isdigit():
        raise ValueError("invalid Enka UID")
    return (await public_get(f"{origin}/api/uid/{uid}")).json()
