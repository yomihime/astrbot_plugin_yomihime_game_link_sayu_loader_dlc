"""Compare isolated adapter frames with the real GsCore handler and resource registry."""

import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
import logging
from pathlib import Path
import re
import sys
import tempfile
from unittest.mock import patch

from native_contract import ROOT, REFERENCE_COMMIT, copy_reference, offline


def segment(kind, data):
    return {"type": kind, "data": data}


def cases():
    base = dict(
        bot_id="fixture:bridge",
        bot_self_id="42",
        msg_id="m1",
        user_type="group",
        group_id="g1",
        user_id="u1",
        sender={"role": "owner", "card": "演示"},
        user_pm=6,
    )

    def frame(*parts, **updates):
        return json.dumps(
            {**base, **updates, "content": [segment(*part) for part in parts]}
        ).encode()

    inner = [segment("text", "内层"), segment("image", "inert")]
    for _ in range(4):
        inner = [segment("node", inner)]
    return {
        "defaults": b"{}",
        "direct_multi_text": frame(
            ("text", " 甲 "),
            ("text", " 乙 "),
            ("text", ""),
            ("text", "  "),
            user_type="direct",
            group_id=None,
        ),
        "pre_reply_images": frame(
            ("reply_id", 1),
            ("image", "https://fixture.invalid/a"),
            ("image", "base64://Yg=="),
            ("at", 42),
            ("text", "询问"),
        ),
        "post_reply_images": frame(
            ("reply", "quoted"),
            ("image", "pre"),
            ("text", "cmd"),
            ("image", "post"),
            ("at", "42"),
            ("at", 7),
        ),
        "reply_image_only": frame(("reply_id", "r"), ("image", "i")),
        "multiple_bot_mentions": frame(
            ("reply_id", 0), ("at", "42"), ("at", "42"), ("text", "cmd")
        ),
        "attachments": frame(
            ("record", "record-one"),
            ("video", "video-one"),
            ("record", "record-two"),
            ("image", "image-one"),
            ("unknown", {"k": 1}),
        ),
        "url_file": frame(("file", "说明.txt|https://fixture.invalid/doc|ignored")),
        "base64_file": frame(("file", "a.txt|YWJj"), user_type="channel"),
        "invalid_file": frame(("file", "missing-separator")),
        "invalid_file_type": frame(("file", 42)),
        "node_preview": frame(
            ("reply_id", "q"),
            (
                "node",
                [
                    segment("text", "转发"),
                    None,
                    segment("file", "any"),
                    *inner,
                    segment("video", "v"),
                ],
            ),
        ),
        "node_existing_reply": frame(("reply", "quoted"), ("node", [])),
        "meta_role_fallback": frame(
            ("meta-joined", {"user_id": "boss", "group_id": 13}),
            user_id="",
            group_id=None,
        ),
        "meta_conflict": frame(
            ("meta-left", {"user_id": "boss", "group_id": "g2", "user_pm": 0})
        ),
        "multiple_meta": frame(
            ("meta-a", {"user_id": "trusted"}),
            ("meta-b", {"group_id": "nested"}),
            user_type="sub_channel",
            user_id="",
            group_id=None,
        ),
        "configured_master": frame(("text", "cmd"), user_id="boss"),
        "configured_superuser": frame(("text", "cmd"), user_id="trusted"),
        "adapter_admin": frame(("text", "cmd"), user_pm=1),
        "claimed_master": frame(("text", "cmd"), user_pm=0),
        "negative_role": frame(("text", "cmd"), user_pm=-1),
        "ignored_extra_fields": frame(
            ("text", "cmd"), WS_BOT_ID="forged", is_tome=True, role="master", user_pm=6
        ),
        "wrong_role_type": frame(("text", "cmd"), user_pm="0"),
        "bad_segment": b'{"content":[1]}',
        "bad_envelope": b"[]",
        "invalid_json": b"{",
    }


def snapshot(event, lookup):
    from msgspec import to_builtins

    value = to_builtins(event)
    value["session_id"] = event.session_id
    aliases = {}
    resources = {}
    for field in ("image_id_list", "audio_id_list", "video_id_list"):
        for index, raw in enumerate(value[field]):
            if not re.fullmatch(r"(img|aud|vid)_[0-9a-f]{16}", raw):
                raise AssertionError("unexpected resource ID")
            stable = f"{raw[:3]}_{index + 1}"
            aliases[raw] = stable
            resources[stable] = lookup(raw)
    for field in ("image_id", "audio_id", "video_id"):
        if value[field] is not None:
            value[field] = aliases[value[field]]
    for field in (
        "image_id_list",
        "audio_id_list",
        "video_id_list",
        "reply_image_id_list",
    ):
        value[field] = [aliases[raw] for raw in value[field]]
    return {"event": value, "resources": resources}


async def compare():
    sys.addaudithook(offline)
    from gsuid_core.utils.database import base_models as db

    await db.init_database()
    from gsuid_core.config import core_config

    core_config.set_config("masters", ["boss"])
    core_config.set_config("superusers", ["trusted"])
    from gsuid_core import handler
    from gsuid_core.bot import _Bot
    from gsuid_core.server import core_shutdown_execute
    from gsuid_core.utils.database.startup import ensure_core_database_tables
    from plugin_inbound import EventResources, TrustedAdapterRoles, decode_adapter_event

    await ensure_core_database_tables()
    connection_id = "fixture-connection"
    ws = _Bot(connection_id, None)
    roles = TrustedAdapterRoles(frozenset({"boss"}), frozenset({"trusted"}))
    observations = {}
    original = handler.msg_process
    for name, wire in cases().items():
        captured = []

        async def observe(msg):
            processed = await original(msg)
            captured.append((deepcopy(processed), processed))
            return processed

        try:
            msg = handler.decode_inbound_frame(wire, connection_id)
            if msg is None:
                native = {"skipped": True}
            else:
                # Observe the original conversion, then let handle_event derive
                # permission and connection identity through its actual branch.
                with patch.object(handler, "msg_process", observe):
                    await handler.handle_event(ws, msg)
                saved, processed = captured.pop()
                saved.WS_BOT_ID, saved.user_pm = processed.WS_BOT_ID, processed.user_pm
                native = snapshot(saved, handler.RM.peek)
        except (IndexError, AttributeError) as exc:
            native = {"error": type(exc).__name__, "detail": str(exc)}
        resources = EventResources()
        try:
            event = decode_adapter_event(
                wire, connection_id=connection_id, resources=resources, roles=roles
            )
            actual = (
                {"skipped": True} if event is None else snapshot(event, resources.peek)
            )
        except (IndexError, AttributeError) as exc:
            actual = {"error": type(exc).__name__, "detail": str(exc)}
        if native != actual:
            raise AssertionError((name, native, actual))
        observations[name] = actual
    ordinary = {}
    for name in (
        "configured_master",
        "configured_superuser",
        "adapter_admin",
        "meta_role_fallback",
        "ignored_extra_fields",
    ):
        event = decode_adapter_event(
            cases()[name], connection_id=connection_id, resources=EventResources()
        )
        assert event.user_pm == 6
        ordinary[name] = event.user_pm
    await core_shutdown_execute()
    await db.engine.dispose()
    return {"cases": observations, "without_role_authority": ordinary}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf8")
    sys.stderr.reconfigure(encoding="utf8")
    with tempfile.TemporaryDirectory(prefix="sayu-inbound-") as tmp:
        work = Path(tmp)
        copy_reference(ROOT / ".sayu-local/reference", work)
        sys.path[:0] = [str(work), str(ROOT / "worker")]
        try:
            results = asyncio.run(compare())
        finally:
            logging.shutdown()
            sys.excepthook = sys.__excepthook__
    results.update(
        reference=REFERENCE_COMMIT,
        decoder_sha256=hashlib.sha256(
            (ROOT / "worker/plugin_inbound.py").read_bytes()
        ).hexdigest(),
    )
    args.output.write_text(
        json.dumps(results, indent=2, ensure_ascii=False), encoding="utf8"
    )
    print(f"Matched {len(results['cases'])} native frame outcomes.")
