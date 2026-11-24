import asyncio
from contextvars import copy_context
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
from plugin_bindings import GsBind, binding_scope
from plugin_mys import GsUser
from plugin_enumeration import EnumerationUnavailable, install_enumeration


def install(**kwargs):
    models = SimpleNamespace(GsBind=GsBind, GsUser=GsUser)
    authority = install_enumeration(models, **kwargs)
    return models, authority


class EnumerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_and_ordinary_contexts_never_open_inventory(self):
        with binding_scope("admin", "adapter", True, writable=True):
            public, disabled = install()
            with self.assertRaises(EnumerationUnavailable):
                await public.GsBind.get_all_uid_list_by_game("adapter")
            with self.assertRaises(EnumerationUnavailable), disabled.delivery():
                self.fail("production acquired an unconfigured grant")
        source = Mock(side_effect=AssertionError("read before authorization"))
        models, authority = install(admins={("admin", "adapter")}, read_rows=source)
        for actor, adapter, private, writable in (
            ("member", "adapter", True, True),
            ("admin", "other", True, True),
            ("admin", "adapter", False, True),
            ("admin", "adapter", True, False),
        ):
            with (
                self.subTest(
                    actor=actor, adapter=adapter, private=private, writable=writable
                ),
                binding_scope(actor, adapter, private, writable=writable),
            ):
                with self.assertRaises(EnumerationUnavailable), authority.delivery():
                    self.fail("foreign role was granted inventory access")
                with self.assertRaises(EnumerationUnavailable):
                    await models.GsUser.get_all_cookie()
        source.assert_not_called()

    async def test_scope_expiry_copy_and_profile_change(self):
        rows = {
            "users": [
                {"cookie": "synthetic-only", "stoken": "test-token", "status": None}
            ],
            "bindings": [{"uid": "one_two", "bot_id": "adapter"}],
        }
        source = Mock(side_effect=rows.__getitem__)
        models, authority = install(admins={("admin", "adapter")}, read_rows=source)
        with binding_scope("admin", "adapter", True, writable=True):
            with self.assertRaises(EnumerationUnavailable):
                await models.GsUser.get_all_cookie()
            with authority.delivery():
                self.assertEqual(
                    await models.GsBind.get_all_uid_list_by_game("adapter"),
                    ["one", "two"],
                )
                self.assertEqual(await models.GsUser.get_all_stoken(), ["test-token"])
                cloned = await models.GsUser.get_all_data()
                cloned[0].cookie = "edited-copy"
                saved = copy_context()
                self.assertEqual(
                    await models.GsUser.get_all_cookie(), ["synthetic-only"]
                )
                with self.assertRaises(EnumerationUnavailable):
                    await asyncio.create_task(models.GsUser.get_all_cookie())
                with (
                    binding_scope("admin", "adapter", True, writable=True),
                    self.assertRaises(EnumerationUnavailable),
                ):
                    await models.GsUser.get_all_cookie()
            # Resume the captured Context on the original task after delivery exit.
            with self.assertRaises(EnumerationUnavailable):
                saved.run(authority.rows, "users", models.GsUser)
            with authority.delivery():
                next_models, _ = install(
                    admins={("admin", "adapter")},
                    read_rows=Mock(side_effect=AssertionError()),
                )
                for model in (models.GsUser, next_models.GsUser):
                    with self.assertRaises(EnumerationUnavailable):
                        await model.get_all_cookie()
            with self.assertRaises(EnumerationUnavailable), authority.delivery():
                self.fail("stale profile issued a new grant")
        self.assertEqual(source.call_count, 4)
