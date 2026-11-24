"""Compare native MiniGG and worker query APIs using only a mocked HTTP transport.

Run with the pinned gsuid_core lock environment. --worker selects a packaged
worker zipapp, otherwise the comparison uses worker sources in this checkout.
"""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import warnings
from unittest.mock import patch

from native_contract import REFERENCE_COMMIT, ROOT, copy_reference, offline


def transport_for(requests):
    from httpx2 import ConnectError, MockTransport, Response

    def handle(request):
        requests.append(
            {"url": str(request.url), "timeout": request.extensions["timeout"]}
        )
        query = request.url.params.get("query")
        if query == "offline":
            raise ConnectError("unavailable", request=request)
        if query == "ambiguous":
            return Response(200, json=["one", "two"])
        if query == "invalid":
            return Response(200, content=b"<html>")
        if query == "retcode":
            return Response(404, json={"retcode": -7})
        if query in ("404", "502", "429"):
            return Response(int(query), json={"error": "example"})
        if request.url.host == "map.minigg.cn":
            if request.url.params.get("map_id") == "missing":
                return Response(404, json={"map": "missing"})
            return Response(
                200, content=b"fixture-image", headers={"Content-Type": "image/jpeg"}
            )
        if request.url.host == "genshin.minigg.cn":
            return Response(200, text="fixture audio")
        return Response(
            200, json={"title": "fixture", "name": query, "costs": {"a": 3}}
        )

    return MockTransport(handle)


async def exercise(api, cache, requests):
    responses = []

    async def call(name, *args, **kwargs):
        start = len(requests)
        try:
            value = await getattr(api, name)(*args, **kwargs)
            response = {"result": value.hex() if isinstance(value, bytes) else value}
        except Exception as exc:
            response = {"exception": type(exc).__name__, "message": str(exc)}
        responses.append({"call": name, **response, "requests": requests[start:]})

    jp = api.APILanguages.JP
    await call(
        "minigg_request",
        "/characters",
        "test",
        jp,
        match_categories=True,
        extra="custom",
    )
    await call("get_character_info", "fixture", cache_path=cache)
    await call("get_character_info", "fixture", result_languages=jp, cache_path=cache)
    await call("get_character_costs", "fixture", cache_path=cache)
    await call("get_character_costs", "ambiguous", cache_path=cache)
    await call("get_character_stats", "fixture", 90)
    await call("get_character_stats", "ambiguous", 1)
    await call("get_character_stats", "fixture", 0)
    await call("get_weapon_info", "ambiguous", cache_path=cache)
    await call("get_weapon_costs", "fixture", jp)
    await call("get_weapon_stats", "fixture", 90)
    await call("get_weapon_stats", "fixture", 91)
    for rank in (None, 0, 1, 6, 7):
        await call("get_constellation_info", "fixture", rank)
    await call("get_talent_info", "ambiguous", cache_path=cache)
    for kind in ("foods", "materials", "domains", "artifacts", "enemies", "other"):
        await call("get_others_info", kind, "fixture")
    for query in ("404", "502", "429", "invalid", "retcode", "offline"):
        await call("get_others_info", "foods", query)
    await call("get_map_data", "ore", "1", True)
    await call("get_map_data", "ore", "missing")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        await call("get_audio_info", "fixture", "line")
    return {
        "calls": responses,
        "deprecated": any(w.category is DeprecationWarning for w in caught),
        "cache": {
            file.relative_to(cache).as_posix(): json.loads(file.read_text("utf8"))
            for file in sorted(cache.rglob("*.json"))
        },
    }


async def compare(work, worker):
    sys.addaudithook(offline)
    from httpx2 import AsyncClient
    from gsuid_core.utils.api.minigg import request as native

    native_requests = []
    native_transport = transport_for(native_requests)
    with patch.object(
        native,
        "AsyncClient",
        lambda **kw: AsyncClient(transport=native_transport, **kw),
    ):
        reference = await exercise(native, work / "native-cache", native_requests)
    sys.path.insert(0, str(worker))
    import plugin_minigg
    from types import ModuleType

    modules = {}

    def package(name):
        modules[name] = ModuleType(name)
        return modules[name]

    plugin_minigg.install_minigg(package, work / "worker-data")
    request = modules["gsuid_core.utils.api.minigg.request"]
    worker_requests = []
    request.minigg_request.__self__.transport = transport_for(worker_requests)
    result = await exercise(
        request, work / "worker-data/resources/test", worker_requests
    )
    if reference != result:
        for index, (original, actual) in enumerate(
            zip(reference["calls"], result["calls"])
        ):
            if original != actual:
                raise AssertionError((index, original, actual))
        raise AssertionError("warning or cache differs")
    for name in plugin_minigg.FUNCTIONS:
        assert getattr(modules["gsuid_core.utils.api.minigg"], name) == getattr(
            request, name
        )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reference", type=Path, default=ROOT / ".sayu-local/reference"
    )
    parser.add_argument("--worker", type=Path, default=ROOT / "worker")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="minigg-reference-") as folder:
        work = Path(folder)
        copy_reference(args.reference, work)
        sys.path.insert(0, str(work))
        result = asyncio.run(compare(work, args.worker.resolve()))
    receipt = {
        "reference_commit": REFERENCE_COMMIT,
        "worker_sha256": hashlib.sha256(args.worker.read_bytes()).hexdigest()
        if args.worker.is_file()
        else None,
        **result,
    }
    if args.output:
        args.output.write_text(
            json.dumps(receipt, indent=2, ensure_ascii=False), "utf8"
        )
    print(
        f"Matched {len(result['calls'])} calls and {len(result['cache'])} cache entries."
    )


if __name__ == "__main__":
    main()
