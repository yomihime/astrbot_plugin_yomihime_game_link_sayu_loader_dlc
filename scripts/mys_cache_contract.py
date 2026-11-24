"""Compare the public cache decorator with the pinned native module."""

import argparse
import asyncio
from base64 import b64encode
import hashlib
from io import BytesIO
import json
import logging
from pathlib import Path
import sys
import tempfile

from build_local import worker_archive
from native_contract import ROOT, REFERENCE_COMMIT, copy_reference, offline


async def exercise(decorator, root):
    from PIL import Image

    out = {}
    calls = {}
    image = Image.new("RGB", (7, 3), (31, 202, 17))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    raw = buffer.getvalue()
    path = root / "sample.png"
    path.write_bytes(raw)
    values = {
        "dict": {"a": 1},
        "zero": 0,
        "error_code": -51,
        "none": None,
        "bytes": raw,
        "encoded": "base64://" + b64encode(raw).decode(),
        "image": image,
        "path": path,
    }

    def get(mode, key, *args, **kwargs):
        index = (mode, key)
        calls[index] = calls.get(index, 0) + 1
        if key == "exception":
            raise ValueError("fixture exception")
        return values[key]

    @decorator(60)
    def sync(key, *args, **kwargs):
        return get("sync", key, *args, **kwargs)

    @decorator(60)
    async def coro(key, *args, **kwargs):
        return get("async", key, *args, **kwargs)

    @decorator(-1)
    def expired(key):
        return get("expired", key)

    def encode(v):
        if isinstance(v, Image.Image):
            return ["image", hashlib.sha256(v.tobytes()).hexdigest()]
        if isinstance(v, (str, bytes, Path)):
            if isinstance(v, Path):
                return ["path", v.name]
            return [
                type(v).__name__,
                hashlib.sha256(v.encode() if isinstance(v, str) else v).hexdigest(),
            ]
        return v

    for mode, fn in (("sync", sync), ("async", coro)):
        for label in [*values, "exception"]:
            results = []
            for arg in ({"a": 1, "b": 2}, {"b": 2, "a": 1}):
                try:
                    result = fn(label, arg, marker=[1, 2])
                    if mode == "async":
                        result = await result
                    results.append(encode(result))
                except Exception as exc:
                    results.append([type(exc).__name__, str(exc)])
            out[f"{mode}/{label}"] = {"calls": calls[(mode, label)], "values": results}
    out["expired"] = [expired("dict"), expired("dict"), calls[("expired", "dict")]]
    return out


async def compare(work, archive):
    sys.addaudithook(offline)
    sys.path.insert(0, str(work))
    from gsuid_core.utils.cache import gs_cache

    native = await exercise(gs_cache, work)
    sys.path.insert(0, str(archive))
    import sayu_compat
    from plugin_bindings import binding_scope
    from plugin_mys_cache import install_cache

    data = work / "loader"
    data.mkdir()
    sayu_compat.install("cache_contract", "cc", data, source_root=work)
    install_cache(sayu_compat.package)
    from gsuid_core.utils.cache import gs_cache as worker_cache

    with binding_scope("actor", "adapter", True):
        actual = await exercise(worker_cache, data)
        count = 0

        @worker_cache()
        def protected(key):
            nonlocal count
            count += 1
            return count

        private_token = "fixture-cookie-do-not-persist"
        if (protected(private_token), protected(private_token)) != (1, 1):
            raise AssertionError("same lease cache miss")
        with binding_scope("another", "adapter", True):
            if protected(private_token) != 2:
                raise AssertionError("cache crossed owners")
        with binding_scope("actor", "adapter", False):
            if (protected(private_token), protected(private_token)) != (3, 4):
                raise AssertionError("unverified context reused private records")
    if any(private_token in str(path) for path in data.rglob("*")):
        raise AssertionError("raw private arguments persisted in a cache path")
    for key, result in native.items():
        if result != actual[key]:
            raise AssertionError((key, result, actual[key]))
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
    spec = json.loads((ROOT / "profiles/lock.json").read_bytes())["genshin"]
    blob = worker_archive(spec)
    with tempfile.TemporaryDirectory(prefix="sayu-cache-") as tmp:
        work = Path(tmp)
        copy_reference(args.reference, work)
        archive = work / "worker.pyz"
        archive.write_bytes(blob)
        try:
            result = asyncio.run(compare(work, archive))
        finally:
            logging.shutdown()
            sys.excepthook = sys.__excepthook__
    evidence = {
        "reference": REFERENCE_COMMIT,
        "worker_sha256": hashlib.sha256(blob).hexdigest(),
        "cases": result,
        "differences": [
            "worker caches per active owner/credential lease",
            "worker holds image entries in memory; raw arguments are never paths",
            "function caches remain separate even when signatures collide",
        ],
    }
    if args.output:
        args.output.write_text(json.dumps(evidence, indent=2), encoding="utf8")
    print(f"Matched {len(result)} native cache cases and confirmed owner isolation.")
