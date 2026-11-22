"""Compare target, attachment and interaction behavior with a pinned native Bot."""

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from native_contract import REFERENCE_COMMIT, copy_reference, offline  # noqa: E402

RECIPIENT = "fixture-room"


async def targeted_cases(bot, segment):
    await bot.target_send("first", "group", RECIPIENT)
    await bot.target_send(
        [segment.text("second"), segment.file(b"\x00fixture\xff", "sample.bin")],
        "group",
        RECIPIENT,
    )


async def interaction_cases(bot, deliver):
    responses = []
    for multi in (False, True):
        method = bot.receive_mutiply_resp if multi else bot.receive_resp
        waiting = asyncio.create_task(method(timeout=2))
        await asyncio.sleep(0)
        user = "second-user" if multi else "origin-user"
        deliver(multi, user)
        event = await waiting
        responses.append([event.user_id, event.text])
    return responses


async def native():
    sys.addaudithook(offline)
    sys.path.insert(0, str(Path.cwd()))
    from gsuid_core.bot import Bot, _Bot
    from gsuid_core.models import Event
    from gsuid_core.segment import MessageSegment
    from starlette.websockets import WebSocketState

    frames = []

    class Adapter:
        application_state = WebSocketState.CONNECTED

        async def send_bytes(self, data):
            packet = json.loads(data)
            frames.append(
                {key: packet[key] for key in ("target_type", "target_id", "content")}
            )

    transport = _Bot("offline-contract", Adapter())
    ev = Event(
        bot_id="astrbot",
        real_bot_id="astrbot",
        bot_self_id="fixture",
        user_type="group",
        group_id="inbound-room",
        user_id="origin-user",
    )
    bot = Bot(transport, ev)
    transport.start_send_worker()
    try:
        await targeted_cases(bot, MessageSegment)
        await asyncio.wait_for(transport._send_queue.join(), 5)

        def deliver(multi, user):
            response = Event(user_id=user, group_id=ev.group_id, text="answer")
            if multi:
                assert bot.get_mutiply_instances()[bot.session_id] is bot
                bot.mutiply_resp.append(response)
                bot.set_mutiply_event()
            else:
                assert bot.get_instances()[bot.session_id] is bot
                bot.resp.append(response)
                bot.set_event()

        responses = await interaction_cases(bot, deliver)
    finally:
        transport._send_task.cancel()
        await asyncio.gather(transport._send_task, return_exceptions=True)
    return {"frames": frames, "interactive": responses}


async def loader():
    sys.path.insert(0, str(ROOT / "worker"))
    from sayu_compat import Bot, Event, MessageSegment, UnsupportedMessage
    from target_transport import BoundTarget

    frames = []

    async def emit(parts):
        frames.append(
            {"target_type": "group", "target_id": RECIPIENT, "content": parts}
        )

    class Interaction:
        def __init__(self):
            self.events = {False: asyncio.Queue(), True: asyncio.Queue()}

        def deliver(self, multi, user):
            self.events[multi].put_nowait(Event(user_id=user, text="answer"))

        async def receive(self, multi):
            return await self.events[multi].get()

    conversation = Interaction()
    bound = Bot(
        target_transport=BoundTarget("group", RECIPIENT, emit), interaction=conversation
    )
    await targeted_cases(bound, MessageSegment)
    responses = await interaction_cases(bound, conversation.deliver)
    production = Bot()
    refused = []
    for name, operation in (
        ("unbound_target", production.target_send("test", "group", RECIPIENT)),
        ("different_target", bound.target_send("test", "group", "other")),
        ("different_kind", bound.target_send("test", "direct", RECIPIENT)),
        ("recall", bound.target_send("test", "group", RECIPIENT, wait_recall=True)),
        (
            "current_reply_file",
            production.send(MessageSegment.file(b"x", "sample.bin")),
        ),
        ("receive_resp", production.receive_resp("prompt", timeout=2)),
        ("receive_mutiply_resp", production.receive_mutiply_resp("prompt", timeout=2)),
    ):
        try:
            await operation
        except UnsupportedMessage:
            refused.append(name)
        else:
            raise AssertionError(f"unexpected success for {name}")
    assert not production.messages and not bound.messages and len(frames) == 2
    return {
        "frames": frames,
        "interactive": responses,
        "explicitly_unsupported": refused,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reference", type=Path, default=ROOT / ".sayu-local/reference"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--native-child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.native_child:
        receipt = asyncio.run(native())
    else:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            sources = copy_reference(args.reference, work)
            output = work / "native.json"
            subprocess.run(
                [
                    sys.executable,
                    "-IB",
                    "-X",
                    "utf8",
                    __file__,
                    "--native-child",
                    "--output",
                    str(output),
                ],
                cwd=work,
                timeout=120,
                check=True,
            )
            observed = json.loads(output.read_bytes())
        projected = asyncio.run(loader())
        if (
            observed["frames"] != projected["frames"]
            or observed["interactive"] != projected["interactive"]
        ):
            raise AssertionError("native and Loader transport observations differ")
        receipt = {
            "reference": REFERENCE_COMMIT,
            "sources": sources,
            "native": observed,
            "loader": projected,
        }
    args.output.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
