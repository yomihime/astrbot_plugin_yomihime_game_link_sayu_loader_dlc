"""Compare plugin event identity, message wire schemas and button construction."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def observe(types, button):
    import msgspec

    base = dict(
        bot_id="adapter",
        WS_BOT_ID="socket",
        bot_self_id="bot",
        user_id="user",
        group_id="room",
    )
    first = types.Event(**base)
    changed = types.Event(**base, raw_text="different", task_id="t2")
    direct = types.Event(**base, user_type="direct")
    first.meta_event_data["reason"] = "fixture"
    first.content.append(types.Message("text", "query"))
    encoded = msgspec.json.encode(first)
    decoded = msgspec.json.decode(encoded, type=types.Event)
    send = types.MessageSend(content=first.content, target_id="room", echo="r1")
    return {
        "fields": {
            name: list(getattr(types, name).__struct_fields__)
            for name in ("Event", "Message", "MessageReceive", "MessageSend")
        },
        "messages": [
            msgspec.to_builtins(x)
            for x in (types.Message(), types.MessageReceive(), first, send)
        ],
        "buttons": [
            msgspec.to_builtins(
                button("go", "cmd", "after", 0, 2, 1, ["role"], ["actor"])
            )
        ],
        "identity": [
            first == changed,
            first == direct,
            hash(first) == hash(changed),
            decoded == first,
            first == object(),
        ],
        "sessions": [first.session_id, direct.session_id, types.Event().session_id],
        "metadata": [
            first.get_meta("reason"),
            changed.get_meta("reason", "unavailable"),
            len(changed.content),
        ],
    }


def segments(api, button, path):
    import msgspec
    from PIL import Image

    segment = api.MessageSegment
    item = button("go", "cmd", prefix="!")
    rows = [[item], []]
    result = {
        "text": segment.text("消息"),
        "links": [
            segment.image(prefix + "fixture")
            for prefix in ("https://", "file://", "base64://")
        ],
        "images": [
            segment.image(value)
            for value in (
                b"media",
                bytearray(b"media"),
                memoryview(b"media"),
                path,
                str(path),
                Image.new("RGBA", (2, 3), (255, 0, 0, 10)),
            )
        ],
        "files": [
            segment.file(value, "名字.bin")
            for value in (path, b"media", "https://fixture.invalid/media")
        ],
        "node": segment.node(
            [
                segment.at("actor"),
                b"binary",
                "https://fixture.invalid/p",
                "base64://opaque",
                "file://cached",
                "plain",
                bytearray(b"ignored"),
            ]
        ),
        "markdown": segment.markdown("text", rows),
        "template": segment.template_markdown("example", {"key": "value"}, rows),
        "empty_buttons": [
            segment.buttons(),
            segment.buttons([]),
            segment.buttons([[]]),
        ],
        "buttons": [
            segment.template_buttons("template"),
            api.process_buttons(rows),
            item.data,
        ],
        "card": segment.card({"title": "fixture"}),
        "size": segment.image_size((2, 3)),
        "log": segment.log("WARNING", "fixture"),
        "media": [
            factory(value)
            for factory in (segment.record, segment.video)
            for value in (path, b"media", bytearray(b"media"), "file://x", "base64://x")
        ],
    }
    operand = segment()
    result["add"] = (operand + item) == [operand, item]
    return msgspec.to_builtins(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from native_contract import ROOT, REFERENCE_COMMIT, copy_reference, offline

    if args.child:
        sys.addaudithook(offline)
        sys.path.insert(0, str(Path.cwd()))
        from gsuid_core import models, segment
        from gsuid_core.message_models import Button

        sys.path.insert(0, str(ROOT / "worker"))
        import plugin_messages
        import sayu_compat

        native = observe(models, Button)
        loader = observe(plugin_messages, plugin_messages.Button)
        path = Path.cwd() / "media.bin"
        path.write_bytes(b"media")
        sayu_compat._data_root = path.parent
        native["segments"] = segments(segment, Button, path)
        loader["segments"] = segments(sayu_compat, plugin_messages.Button, path)
        assert native == loader
        args.output.write_text(
            json.dumps(
                {"reference": REFERENCE_COMMIT, "native": native, "loader": loader},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf8",
        )
        return
    with tempfile.TemporaryDirectory(prefix="sayu-event-") as directory:
        work = Path(directory)
        copy_reference(args.reference.resolve(), work)
        subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-X",
                "utf8",
                str(Path(__file__).resolve()),
                "--child",
                "--reference",
                str(work),
                "--output",
                str(args.output.resolve()),
            ],
            cwd=work,
            check=True,
        )


if __name__ == "__main__":
    main()
