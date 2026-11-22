import asyncio
import base64
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
import sayu_compat as host


class PortsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.install()

    def install(self):
        host.install(
            "Example", "ex", self.root / "data", source_root=self.root / "source"
        )

    def dispatch(self, text, **event):
        return asyncio.run(host.dispatch(text, **event))

    def test_order_aliases_block_and_event_fields(self):
        service = host.SV("reader")
        calls = []

        @service.on_prefix(("a", "ab"))
        async def prefix(bot, ev):
            calls.append(ev)
            await bot.send(ev.text)

        @service.on_command("abc")
        async def command(bot, ev):
            calls.append(ev)
            await bot.send(ev.command)

        @service.on_regex("b(?P<value>c)")
        async def regex(bot, ev):
            calls.append(ev)
            await bot.send(ev.regex_dict["value"])

        @service.on_fullmatch("abc", block=True)
        async def exact(bot, ev):
            calls.append(ev)
            await bot.send("one")

        self.assertEqual(self.dispatch("exabc"), [{"text": "one"}])
        parts = self.dispatch("ex abc d", actor_id="A", group_id="G", message_id="M")
        self.assertEqual(
            parts, [{"text": "abc"}, {"text": "c d"}, {"text": "bc d"}, {"text": "c"}]
        )
        self.assertEqual(calls[-1].regex_group, ("c",))
        self.assertEqual(
            (calls[-1].user_id, calls[-1].user_type, calls[-1].msg_id),
            ("A", "group", "M"),
        )
        self.assertEqual(calls[-1].raw_text, "ex abc d")
        self.assertIsNot(calls[-1], calls[-2])

    def test_gates_prefix_and_area(self):
        for service in (
            host.SV("restricted", pm=2),
            host.SV("disabled", enabled=False),
            host.SV("permission list", white_list=["A"]),
        ):

            @service.on_command("example")
            async def rejected(bot, ev):
                self.fail("ineligible service executed")

        sv = host.SV("direct", area="DIRECT")

        @sv.on_prefix("example", block=True)
        async def direct(bot, ev):
            await bot.send(ev.text)

        for text, event in (
            ("example arg", {"conversation_kind": "direct"}),
            ("exexample", {"conversation_kind": "direct"}),
            ("exexample arg", {}),
            ("exexample arg", {"group_id": "g"}),
        ):
            with self.subTest(text=text, event=event), self.assertRaises(host.NoMatch):
                self.dispatch(text, **event)
        self.assertEqual(
            self.dispatch("exexample arg", conversation_kind="direct"),
            [{"text": "arg"}],
        )

    def test_keyword_tie_preserves_registration_order(self):
        service = host.SV("keywords")

        @service.on_keyword("a", block=True)
        async def short(bot, ev):
            await bot.send("first")

        @service.on_keyword("ab", block=True)
        async def long(bot, ev):
            await bot.send("second")

        self.assertEqual(self.dispatch("exabc"), [{"text": "first"}])

    def test_plugin_authorization_applies_to_public_services(self):
        host.Plugins("Example", pm=2, force_prefix=["ex"])

        @host.SV("public").on_command("ping")
        async def ping(bot, ev):
            self.fail("privileged plugin executed")

        with self.assertRaises(host.NoMatch):
            self.dispatch("exping")

    def test_suffix_and_priority(self):
        @host.SV("early", priority=1).on_suffix("tail", prefix=False, block=True)
        async def first(bot, ev):
            await bot.send(ev.text)

        self.assertEqual(self.dispatch("body tail"), [{"text": "body"}])
        with self.assertRaises(host.NoMatch):
            self.dispatch("tail")

    def test_registration_rollback_and_reinstall(self):
        async def handler(bot, ev):
            await bot.send("handled")

        with self.assertRaises(ValueError):
            with host.registration_scope():
                host.SV("pending").on_command("sample")(handler)
                raise ValueError("failed initializer")
        with self.assertRaises(host.NoMatch):
            self.dispatch("exsample")
        host.SV("enabled").on_command(("sample", "alias"))(handler)
        self.assertEqual(
            host.registration_counts(), {"services": 1, "handlers": 1, "triggers": 2}
        )
        self.assertEqual(self.dispatch("exsample"), [{"text": "handled"}])
        self.install()
        with self.assertRaises(host.NoMatch):
            self.dispatch("exsample")

    def test_ordered_nodes_images_and_unsupported_output(self):
        pixel = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAA"
            "C0lEQVR42mP8/x8AAwMCAO+jgd8AAAAASUVORK5CYII="
        )
        encoded = base64.b64encode(pixel).decode("ascii")

        @host.SV("segments").on_command("sample", block=True)
        async def segments(bot, ev):
            if ev.text:
                await bot.send(host.MessageSegment.file(pixel, "file.png"))
                return
            await bot.send("before")
            await bot.send(
                host.MessageSegment.node(
                    [
                        host.MessageSegment.text("middle"),
                        host.MessageSegment.image("base64://" + encoded),
                    ]
                )
            )
            await bot.send_option("after", [[host.Button("🧪", "selection")]])

        self.assertEqual(
            self.dispatch("exsample"),
            [
                {"text": "before"},
                {"text": "middle"},
                {"image": encoded},
                {"text": "after"},
                {"text": "exselection"},
            ],
        )
        with self.assertRaises(host.UnsupportedMessage):
            self.dispatch("exsample file")

    def test_thread_port_resolves_coroutines_on_another_thread(self):
        import threading

        @host.to_thread
        async def compute():
            await asyncio.sleep(0)
            return threading.get_ident()

        self.assertNotEqual(asyncio.run(compute()), threading.get_ident())

    def test_adapter_log_is_separate_from_chat(self):
        bot = host.Bot()
        asyncio.run(bot.logger.info("fixture diagnostic"))
        self.assertEqual(bot.messages, [])
        self.assertEqual(host.adapter_log_counts(), {"INFO": 1})
        self.assertEqual(host._adapter_frames[-1], ("INFO", "fixture diagnostic"))

    def test_resources_and_public_prefix(self):
        expected = self.root / "data" / "resources" / "Example" / "nested"
        self.assertEqual(host.get_res_path(["Example", "nested"]), expected)
        self.assertTrue(expected.is_dir())
        for part in ("..", "../another", str(self.root.resolve())):
            with self.assertRaises(ValueError):
                host.get_res_path(["Example", part])
        self.assertEqual(host.get_plugin_available_prefix("Example"), "ex")
        with self.assertRaises(KeyError):
            host.get_plugin_available_prefix("another")
