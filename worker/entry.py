"""Run one chat event against an empty, private instance of the upstream registry."""

import asyncio
import contextlib
import json
import sys
from collections import Counter
from pathlib import Path

import sayu_compat
from declarations import Unsupported
from discovery import discover
from game_profile import PACKAGE, PREFIX
from images import prepare_owner_parts, prepare_public_parts


async def handle(source, request):
    data_root = Path.cwd() / "data"
    data_root.mkdir()
    sayu_compat.install(PACKAGE, PREFIX, data_root, source_root=source)
    if request.get("op") == "inventory":
        skipped = Counter()
        await discover(source, PACKAGE, skipped=skipped)
        report = {
            "registered": sayu_compat.registration_counts(),
            "skipped": dict(skipped),
        }
        if request.get("list_triggers"):
            report["triggers"] = sayu_compat.registered_triggers()
        return report
    await discover(source, PACKAGE)
    parts = await sayu_compat.dispatch(request["text"], **request["event"])
    if request["private_image"]:
        parts = prepare_owner_parts(parts, source, PACKAGE)
    else:
        parts = prepare_public_parts(parts)
    return {"parts": parts}


def main():
    try:
        request = json.loads(sys.stdin.buffer.read(8192))
        with contextlib.redirect_stdout(sys.stderr):
            reply = asyncio.run(handle(Path(sys.argv[1]), request))
    except sayu_compat.NoMatch:
        reply = {"error": "no_match"}
    except sayu_compat.UnsupportedMessage:
        reply = {"error": "unsupported_message"}
    except (ImportError, NotImplementedError, Unsupported):
        reply = {"error": "missing_port"}
    except Exception:
        reply = {"error": "upstream_error"}
    sys.stdout.buffer.write(json.dumps(reply, ensure_ascii=True).encode("ascii"))


if __name__ == "__main__":
    main()
