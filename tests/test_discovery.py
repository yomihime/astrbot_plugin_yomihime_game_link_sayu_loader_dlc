"""Registration, startup data and capability boundaries using invented services."""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from textwrap import dedent

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
from discovery import discover
import sayu_compat as host


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / "source"
        self.plugin = self.source / "Example"
        self.plugin.mkdir(parents=True)
        self.write(
            "__init__.py", "raise AssertionError('application startup executed')"
        )
        self.install()

    def install(self):
        host.install("Example", "ex", self.root / "data", source_root=self.source)

    def write(self, path, text):
        path = self.plugin / path
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_text(dedent(text), encoding="utf-8")

    def load(self):
        return asyncio.run(discover(self.source, "Example"))

    def send(self, command):
        return asyncio.run(host.dispatch("ex" + command, conversation_kind="direct"))

    def test_discover_imported_declarations_and_initialize_resources(self):
        self.write(
            "tables/__init__.py",
            """
            import json
            import aiofiles
            from pathlib import Path
            from gsuid_core.server import on_core_start
            data = {}
            root = Path(__file__).parent

            @on_core_start
            async def load():
                async with aiofiles.open(root / "fruit.json", "rb") as f:
                    data.update(json.loads(await f.read()))

            def lookup(name):
                return data[name]
        """,
        )
        self.write("tables/fruit.json", '{"apple":"orchard"}')
        self.write(
            "menu.py",
            """
            from gsuid_core.sv import SV
            from .tables import lookup
            menu = SV("menu")
            @menu.on_command(("where", "source"))
            async def fruit(bot, event):
                await bot.send(lookup(event.text))
        """,
        )
        for _ in range(2):
            self.install()
            self.assertEqual([f.__name__ for f in self.load()], ["fruit"])
            self.assertEqual(self.send("source apple"), [{"text": "orchard"}])
        self.write("tables/fruit.json", "malformed")
        self.install()
        self.assertEqual(self.load(), ())

    def test_unrelated_side_effects_and_privileged_callbacks_are_inert(self):
        self.write(
            "mixed.py",
            """
            import os
            from pathlib import Path
            from gsuid_core.sv import SV

            root = Path(__file__).parent
            public = SV("public")
            admin = SV("admin", pm=1)
            os.system("unrelated-initializer")
            @public.on_fullmatch("yes")
            async def plain(bot, ev):
                await bot.send("public")
            @public.on_command("write")
            async def writable(bot, ev):
                open(root / ev.text, "w").write("x")
            @public.on_fullmatch("system")
            async def command(bot, ev):
                os.system(ev.text)
            @admin.on_fullmatch("restricted")
            async def restricted(bot, ev):
                await bot.send("admin")
        """,
        )
        self.assertEqual([f.__name__ for f in self.load()], ["plain"])
        for command in ("write abc", "system", "restricted"):
            with self.subTest(command=command), self.assertRaises(host.NoMatch):
                self.send(command)

    def test_runtime_input_cannot_escape_packaged_assets(self):
        self.write(
            "assets.py",
            """
            from pathlib import Path
            from gsuid_core.sv import SV
            root = Path(__file__).parent
            public = SV("assets")
            @public.on_command("lookup")
            async def lookup(bot, ev):
                if (root / ev.text).is_file():
                    await bot.send("available")
        """,
        )
        self.assertEqual(len(self.load()), 1)
        with self.assertRaisesRegex(ValueError, "outside plugin assets"):
            self.send("lookup ../../outside.json")

    def test_module_resource_assignments_and_updates_are_linked_on_demand(self):
        self.write(
            "tables/catalog.py",
            """
            import json
            from pathlib import Path
            root = Path(__file__).parents[1]
            index_file = root / "items.json"
            with index_file.open(encoding="utf-8") as stream:
                items = json.load(stream)
            labels = {}
            try:
                with open(root / "labels.json", "rb") as stream:
                    labels.update(json.loads(stream.read()))
                with open(root / "extra.json", "rb") as stream:
                    labels.update(json.load(stream))
            except FileNotFoundError:
                pass
            protected = {}
            try:
                assert False, "integrity check"
                with open(root / "labels.json", "rb") as stream:
                    protected.update(json.load(stream))
            except OSError:
                pass
            # An unrelated tool's initialization must not be run on discovery.
            with open(root / "forbidden", "w") as stream:
                unrelated = stream.write("should not exist")
            with index_file.open("w") as stream:
                overwritten = json.load(stream)
        """,
        )
        self.write(
            "reader.py",
            """
            from gsuid_core.sv import SV
            from .tables.catalog import labels, items, protected, overwritten
            menu = SV("reader")
            @menu.on_command("item")
            async def item(bot, ev):
                await bot.send(items[ev.text])
            @menu.on_command("overwritten")
            async def overwrite(bot, ev):
                await bot.send(overwritten[ev.text])
            @menu.on_command("protected")
            async def protected_label(bot, ev):
                await bot.send(protected[ev.text])
            @menu.on_command("label")
            async def read_label(bot, ev):
                await bot.send(labels[ev.text])
            @menu.on_command("labels")
            async def list_labels(bot, ev):
                await bot.send(",".join(sorted(labels)))
        """,
        )
        self.write("items.json", '{"tea":"green"}')
        self.write("labels.json", '{"price":"euros"}')
        self.write("extra.json", '{"place":"orchard"}')
        self.assertEqual(len(self.load()), 3)
        self.assertEqual(self.send("item tea"), [{"text": "green"}])
        self.assertEqual(self.send("label place"), [{"text": "orchard"}])
        self.assertFalse((self.plugin / "forbidden").exists())

        self.write("extra.json", "invalid json")
        self.install()
        self.assertEqual([f.__name__ for f in self.load()], ["item"])
        with self.assertRaises(host.NoMatch):
            self.send("label price")

    def test_optional_local_call_and_function_imports_use_real_declarations(self):
        self.write(
            "tools.py",
            """
            import subprocess
            def unavailable():
                subprocess.run(["unavailable"])
            def label(value):
                return value.upper()
        """,
        )
        self.write(
            "menu.py",
            """
            from gsuid_core.sv import SV
            from .tools import unavailable
            menu = SV("menu")
            @menu.on_command("preview")
            async def preview(bot, ev):
                try:
                    result = unavailable()
                except ValueError:
                    from .tools import label
                    result = label(ev.text)
                await bot.send(result)
            @menu.on_command("required")
            async def required(bot, ev):
                await bot.send(unavailable())
        """,
        )
        self.assertEqual([f.__name__ for f in self.load()], ["preview"])
        self.assertEqual(self.send("preview green"), [{"text": "GREEN"}])
        with self.assertRaises(host.NoMatch):
            self.send("required green")

    def test_private_json_and_in_memory_drawing(self):
        self.write(
            "cache.py",
            """
            import json
            from pathlib import Path
            from gsuid_core.data_store import get_res_path
            from PIL import Image, ImageDraw
            from gsuid_core.sv import SV
            cache = get_res_path("Example") / "derived" / "data.json"
            entries = {}
            svc = SV("palette")
            def refresh():
                global entries
                entries = {"blue": [2, 6, 220]}
                with open(cache, "w", encoding="utf-8") as out:
                    out.write(json.dumps(entries))
            @svc.on_command("square")
            async def draw_square(bot, ev):
                refresh()
                with open(cache, encoding="utf-8") as restored:
                    colors = json.load(restored)
                image = Image.new("RGB", (24, 20))
                drawing = ImageDraw.Draw(image)
                drawing.rectangle([1, 1, 20, 16], fill=tuple(colors[ev.text]))
                await bot.send(image)
        """,
        )
        self.assertEqual(len(self.load()), 1)
        import base64
        from io import BytesIO
        from PIL import Image

        reply = self.send("square blue")
        with Image.open(BytesIO(base64.b64decode(reply[0]["image"]))) as image:
            self.assertEqual(image.size, (24, 20))
            self.assertEqual(image.getpixel((5, 5)), (2, 6, 220))
        self.assertEqual(list(self.plugin.glob("*.json")), [])

    def test_packed_asset_checks_actual_materialization(self):
        from unittest.mock import Mock
        from discovery import Assets

        assets = Assets(self.source)
        assets.data_root = self.root / "data"
        assets.public = Mock()
        path = assets.path(assets.data_root / "public.png")
        assets.public.ensure.return_value = True
        self.assertFalse(path.exists())

        def materialize(_, destination):
            destination.parent.mkdir(exist_ok=True, parents=True)
            destination.write_bytes(b"resource")
            return True

        assets.public.ensure.side_effect = materialize
        self.assertTrue(path.is_file())
        with path.open("rb") as stream:
            self.assertEqual(stream.read(), b"resource")
