"""Compare plugin AI declarations with the fixed native AI configuration switched off."""

import argparse
import asyncio
from dataclasses import fields
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build_local import worker_archive  # noqa: E402
from native_contract import REFERENCE_COMMIT, copy_reference, offline  # noqa: E402


async def observe(kind, archive):
    sys.addaudithook(offline)
    if kind == "native":
        sys.path.insert(0, str(Path.cwd()))
    else:
        sys.path.insert(0, str(archive))
        from sayu_compat import install

        install("fixture", "", Path.cwd() / "data")
    from gsuid_core.ai_core.configs.ai_config import ai_config
    from gsuid_core.ai_core.models import KnowledgePoint, ImageEntity, ToolContext
    from gsuid_core.ai_core import register
    from gsuid_core.ai_core.rag import query_knowledge
    from gsuid_core.ai_core.rag.knowledge import sync_knowledge

    flag = ai_config.get_config("enable")
    assert flag.data is False
    calls = []

    async def original(ctx: ToolContext, key: str):
        calls.append(key)
        return ctx.extra[key]

    direct = register.ai_tools(original)
    configured = register.ai_tools(
        category="common", context_tags=["fixture"], capability_domain="local"
    )(original)
    assert direct is original and configured is original
    entity = KnowledgePoint(
        id="fixture", plugin="fixture", title="Example", content="test", tags=[]
    )
    item = ImageEntity(
        id="picture",
        plugin="fixture",
        path="fixture.png",
        tags=[],
        content="test",
        source="fixture",
        _hash=None,
    )
    registry = [
        register.ai_entity(entity),
        register.ai_image(item),
        register.ai_alias("one", "alias"),
        register.ai_alias("two", ["alias"], scope="fixture"),
    ]
    answer = await configured(ToolContext(extra={"key": "read"}), "key")
    return {
        "flag": [flag.title, flag.desc, flag.data],
        "context_fields": [f.name for f in fields(ToolContext)],
        "registry_calls": registry,
        "entity": entity,
        "image": item,
        "calls": calls,
        "result": answer,
        "entity_count": len(register._ENTITIES),
        "image_count": len(register._IMAGE_ENTITIES),
        "tools": sorted(register.get_all_tools()),
        "scoped_aliases": register.get_aliases_for_scope("fixture"),
        "sync": await sync_knowledge(),
        "query": await query_knowledge("example", limit=2, plugin_filter=["fixture"]),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--loader-python", type=Path, default=Path(sys.executable))
    parser.add_argument(
        "--reference", type=Path, default=ROOT / ".sayu-local/reference"
    )
    parser.add_argument("--child", choices=("loader", "native"))
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    if args.child:
        result = asyncio.run(observe(args.child, args.archive))
    else:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = copy_reference(args.reference.resolve(), root / "native")
            (root / "loader").mkdir()
            archive = root / "worker.pyz"
            archive.write_bytes(
                worker_archive(
                    json.loads((ROOT / "profiles/lock.json").read_bytes())["waves"]
                )
            )
            result = {
                "reference": REFERENCE_COMMIT,
                "manifest": manifest,
                "worker_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            }
            for kind, exe in (
                ("native", sys.executable),
                ("loader", args.loader_python.resolve()),
            ):
                output = root / (kind + ".json")
                subprocess.run(
                    [
                        str(exe),
                        "-IB",
                        "-X",
                        "utf8",
                        __file__,
                        "--child",
                        kind,
                        "--archive",
                        str(archive),
                        "--output",
                        str(output),
                    ],
                    cwd=root / kind,
                    check=True,
                    timeout=90,
                )
                result[kind] = json.loads(output.read_bytes())
    args.output.write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf8"
    )
    if not args.child and result["native"] != result["loader"]:
        raise SystemExit("disabled native and Loader observations differ")


if __name__ == "__main__":
    main()
