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
