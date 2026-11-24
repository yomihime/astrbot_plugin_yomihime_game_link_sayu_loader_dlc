import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
from sayu_compat import Bot, Message, MessageSegment, UnsupportedMessage
from target_transport import BoundTarget


class TargetTransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_attachment_failure_has_no_partial_reply_or_target_send(self):
        emit = AsyncMock()
        bot = Bot(target_transport=BoundTarget("direct", "recipient", emit))
        await bot.send("before")
        with self.assertRaises(UnsupportedMessage):
            await bot.send(["partial reply", MessageSegment.file(b"x", "x.bin")])
        self.assertEqual(bot.messages, [{"text": "before"}])
        self.assertEqual(bot.text_size, len("before"))
        for message in (
            Message("file", "bad|link://example"),
            Message("file", "../escape|YQ=="),
            Message("file", "short|abc"),
            Message("at", "forged"),
        ):
            with self.assertRaises(UnsupportedMessage):
                await bot.target_send(["partial", message], "direct", "recipient")
        with self.assertRaises(UnsupportedMessage):
            await bot.target_send("test", "direct", "other")
        emit.assert_not_awaited()
        with self.assertRaises(UnsupportedMessage):
            MessageSegment.file(Path("local.txt"), "fixture.bin")
        link = MessageSegment.file("https://example.invalid/resource", "fixture.bin")
        self.assertEqual(link.data, "fixture.bin|link://https://example.invalid/resource")
        with self.assertRaises(UnsupportedMessage):
            await bot.target_send(link, "direct", "recipient")
        emit.assert_not_awaited()

    async def test_transport_failures_propagate(self):
        emit = AsyncMock(side_effect=OSError("offline"))
        bot = Bot(target_transport=BoundTarget("direct", "recipient", emit))
        with self.assertRaises(OSError):
            await bot.target_send("test", "direct", "recipient")
        self.assertEqual(bot.messages, [])

    async def test_interaction_timeout_and_unbound_rejection(self):
        class Interaction:
            prompt = AsyncMock()

            async def receive(self, multi):
                await asyncio.Event().wait()

        interaction = Interaction()
        bot = Bot(interaction=interaction)
        self.assertIsNone(await bot.receive_resp("question", timeout=0.01))
        interaction.prompt.assert_awaited_once_with("question")
        with self.assertRaises(TimeoutError):
            await bot.receive_mutiply_resp(timeout=0.01)
        with self.assertRaises(UnsupportedMessage):
            await Bot().receive_resp("question")
