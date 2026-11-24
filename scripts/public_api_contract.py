"""Compare public data providers at the HTTP transport boundary with native GsCore."""

import argparse
import asyncio
import importlib
import json
import logging
from io import BytesIO
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

from native_contract import ROOT, REFERENCE_COMMIT, copy_reference, offline


async def exercise(cache_name):
    import httpx2
    from gsuid_core.data_store import get_res_path
    from PIL import Image

    requests = []
    payload = None
    status = 200

    def transport(request):
        requests.append(
            {
                "method": request.method,
                "url": str(request.url),
                "user_agent": request.headers.get("user-agent"),
            }
        )
        return httpx2.Response(
            status,
            content=payload.encode("utf8") if isinstance(payload, str) else payload,
        )

    client = httpx2.AsyncClient
    with patch.object(
        httpx2,
        "AsyncClient",
        lambda **kw: client(transport=httpx2.MockTransport(transport), **kw),
    ):
        from gsuid_core.utils.api.akashadata import request as akasha
        from gsuid_core.utils.api.ambr import request as ambr
        from gsuid_core.utils.api.enka import request as enka
        from gsuid_core.utils.api.hhw import request as hhw

        cache = get_res_path(cache_name)
        daily = '{"code":200,"data":{"monday":{"alpha":{"id":11},"beta":{"id":12}}}}'
        items = '{"code":200,"data":{"items":{"A":{"name":"example"}}}}'
        details = '{"code":200,"data":{"records":[1,2]}}'
        html = (
            '<section id="Variant #1">'
            + "".join(f"<td>Monsters</td><td><a>enemy-{i}</a></td>" for i in range(6))
            + "</section>"
        )

        results = []
        for label, function, text in (
            (
                "abyss",
                akasha.get_akasha_abyss_info,
                'var static_abyss_total ={"team_list":[{"ac":2}]}',
            ),
            (
                "character",
                akasha.get_akasha_all_char_info,
                'var static_card_details ={"1":{"weapons":"[1,2]"}}',
            ),
            (
                "rank",
                akasha.get_akasha_abyss_rank,
                'var static_schedule_version_dict ={"a":1};var static_abyss_record_dict ={"usage_list":[1]}',
            ),
            (
                "schedule",
                lambda: akasha.get_akasha_abyss_rank(True),
                'var static_schedule_version_dict ={"a":1};var static_abyss_record_dict ={"usage_list":[1]}',
            ),
            ("bad_rank", akasha.get_akasha_abyss_rank, "{}"),
            ("malformed", akasha.get_akasha_abyss_info, "{"),
            ("ambr_daily", ambr.get_ambr_daily_data, daily),
            ("ambr_event", ambr.get_ambr_event_info, '{"event":1}'),
            ("char_list", ambr.get_ambr_char_list, items),
            ("weapon_list", ambr.get_ambr_weapon_list, items),
            ("relic_list", ambr.get_ambr_reliquary_list, items),
            (
                "char_data",
                lambda: ambr.get_ambr_char_data(9, cache_path=cache),
                details,
            ),
            (
                "char_cache_hit",
                lambda: ambr.get_ambr_char_data(9, cache_path=cache),
                "{}",
            ),
            (
                "weapon_data",
                lambda: ambr.get_ambr_weapon_data(9, cache_path=cache),
                details,
            ),
            (
                "relic_data",
                lambda: ambr.get_ambr_reliquary_data(9, cache_path=cache),
                details,
            ),
            (
                "monster_data",
                lambda: ambr.get_ambr_monster_data(9, cache_path=cache),
                details,
            ),
            (
                "gcg_detail",
                lambda: ambr.get_ambr_gcg_detail(9, cache_path=cache),
                details,
            ),
            ("gcg_list", ambr.get_ambr_gcg_list, details),
            ("monster_list", ambr.get_ambr_monster_list, details),
            ("upgrade", ambr.get_all_upgrade, details),
            ("book_list", ambr.get_all_book_id, items),
            ("book", lambda: ambr.get_book_volume(1), details),
            ("story", lambda: ambr.get_story_data(1), '{"code":200,"data":"story"}'),
            ("ambr_error", ambr.get_all_upgrade, '{"code":404}'),
            ("ambr_bad_schema", ambr.get_all_upgrade, "{}"),
            ("enka", lambda: enka.get_enka_info("100000001"), '{"ttl":20}'),
            ("hhw_raw", hhw.get_abyss_review_raw, html),
            ("hhw_parse", lambda: hhw.get_abyss_review(html.encode(), 1, 12), ""),
            ("hhw_missing", lambda: hhw.get_abyss_review(html.encode(), 99, 12), ""),
        ):
            payload = text
            try:
                value = await function()
                result = {
                    "value": value.decode() if isinstance(value, bytes) else value
                }
            except Exception as exc:
                result = {"error": type(exc).__name__, "message": str(exc)}
            results.append({"case": label, **result})
        icon_buffer = BytesIO()
        Image.new("RGBA", (3, 7), (9, 1, 33, 255)).save(icon_buffer, "PNG")
        for case, status, filename, ui in (
            ("icon", 200, "sample", "ItemIcon"),
            ("icon_cached", 404, "sample", "ItemIcon"),
            ("icon_absent", 404, "other", None),
        ):
            payload = icon_buffer.getvalue()
            icon = await ambr.get_ambr_icon("UI", filename, cache, ui_name=ui)
            results.append(
                {
                    "case": case,
                    "value": [icon.mode, list(icon.size), list(icon.getpixel((0, 0)))],
                }
            )
            icon.close()
        return {"requests": requests, "cases": results}


async def compare(work):
    sys.addaudithook(offline)
    sys.path.insert(0, str(work))
    original = await exercise("native-fixture")
    providers = ("akashadata", "ambr", "enka", "hhw")
    for name in list(sys.modules):
        if any(
            name.startswith("gsuid_core.utils.api." + provider)
            for provider in providers
        ):
            del sys.modules[name]
    sys.path.insert(0, str(ROOT / "worker"))
    from plugin_public_api import install_public_api

    api = importlib.import_module("gsuid_core.utils.api")
    api.__path__ = []
    for provider in providers:
        if hasattr(api, provider):
            delattr(api, provider)
    install_public_api()
    actual = await exercise("worker-fixture")
    if actual != original:
        raise ValueError((original, actual))
    return actual


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf8")
    sys.stderr.reconfigure(encoding="utf8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reference", type=Path, default=ROOT / ".sayu-local/reference"
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="public-providers-") as tmp:
        work = Path(tmp)
        copy_reference(args.reference, work)
        try:
            data = asyncio.run(compare(work))
        finally:
            logging.shutdown()
            sys.excepthook = sys.__excepthook__
    if args.output:
        args.output.write_text(
            json.dumps({"reference": REFERENCE_COMMIT, **data}, indent=2),
            encoding="utf8",
        )
    print(f"Matched {len(data['cases'])} public API cases.")
