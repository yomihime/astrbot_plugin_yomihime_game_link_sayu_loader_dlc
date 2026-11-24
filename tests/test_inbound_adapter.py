import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
import sayu_compat as host
from plugin_inbound import EventResources, TrustedAdapterRoles, decode_adapter_event


class InboundAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        host.install("InboundFixture", "ib", Path(folder.name), runtime_mode="package")

    def decode(self, segments, roles=None):
        wire = json.dumps(
            {
                "bot_id": "fixture:adapter",
                "bot_self_id": "bot",
                "sender": {"role": "owner"},
                "user_type": "group",
                "user_pm": 0,
                "content": [{"type": kind, "data": value} for kind, value in segments],
            }
        ).encode()
        return decode_adapter_event(
            wire, connection_id="fixture-ws", resources=EventResources(), roles=roles
        )

    async def test_file_and_forwarded_text_reach_registered_plugin(self):
        service = host.SV("file-inbound")

        @service.on_file("txt")
        async def receive(bot, event):
            await bot.send(
                f"{event.file_name} {event.file_type} {event.raw_text} {event.reply}"
            )

        event = self.decode(
            [
                ("text", " 内容 "),
                ("file", "a.txt|Yg=="),
                ("reply_id", "r"),
                ("node", [{"type": "text", "data": "转发"}]),
            ]
        )
        self.assertEqual(
            await host.dispatch_event(event, mapped=True),
            [{"text": "a.txt base64 内容 [合并转发]\n转发"}],
        )
        self.assertEqual(event.real_bot_id, "fixture:adapter")
        self.assertEqual(event.WS_BOT_ID, "fixture-ws")

    async def test_meta_identity_uses_only_explicit_authority_for_privilege(self):
        service = host.SV("privileged-inbound", pm=0)

        @service.on_meta("joined")
        async def receive(bot, event):
            await bot.send(f"{event.user_id} {event.group_id} {event.user_pm}")

        parts = [("meta-joined", {"user_id": "fixture-owner", "group_id": 7})]
        with self.assertRaises(host.NoMatch):
            await host.dispatch_event(self.decode(parts), mapped=True)
        roles = TrustedAdapterRoles(masters=frozenset({"fixture-owner"}))
        self.assertEqual(
            await host.dispatch_event(self.decode(parts, roles), mapped=True),
            [{"text": "fixture-owner 7 0"}],
        )
        with self.assertRaises(host.NoMatch):
            await host.dispatch(
                "meta-joined", actor_id="fixture-owner", conversation_kind="group"
            )

    async def test_sdk_text_preserves_segments_and_direct_addressing(self):
        service = host.SV("text-address")
        observed = []

        @service.on_command("inspect", to_me=True)
        async def inspect_message(bot, ev):
            observed.append(ev)
            await bot.send(ev.text)

        text = "  ibinspect @other [图片]  "
        args = dict(actor_id="opaque-actor", message_id="opaque-message")
        self.assertEqual(
            await host.dispatch(text, conversation_kind="direct", **args),
            [{"text": "@other [图片]"}],
        )
        event = observed.pop()
        self.assertEqual(event.raw_text, text.strip())
        self.assertEqual([(m.type, m.data) for m in event.content], [("text", text)])
        self.assertTrue(event.is_tome)
        self.assertFalse(event.at_bot)
        self.assertEqual((event.at, event.image, event.user_pm), (None, None, 6))
        self.assertEqual(
            (event.user_id, event.msg_id), ("opaque-actor", "opaque-message")
        )
        for kind in ("group", ""):
            with self.subTest(kind=kind), self.assertRaises(host.NoMatch):
                await host.dispatch(text, conversation_kind=kind, **args)
        self.assertEqual(observed, [])

        await host.dispatch(
            text, adapter_id="sdk:instance", conversation_kind="direct", **args
        )
        self.assertEqual(observed.pop().bot_id, "sdk:instance")

    async def test_nested_error_reply_stays_with_its_original_command(self):
        from gsuid_core.bot import call_bot

        async def send_error():
            await asyncio.sleep(0)
            current = call_bot()
            await current.send(await current.t("fixture error {e}", e=current.ev.text))

        @host.SV("errors").on_command("error")
        async def callback(bot, ev):
            await send_error()

        replies = await asyncio.gather(
            host.dispatch("iberror first", actor_id="one", conversation_kind="direct"),
            host.dispatch("iberror second", actor_id="two", conversation_kind="direct"),
        )
        self.assertEqual(
            replies,
            [[{"text": "fixture error first"}], [{"text": "fixture error second"}]],
        )
