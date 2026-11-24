import asyncio
from contextvars import copy_context
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
from plugin_bindings import GsBind, binding_scope
from plugin_mys import GsUser, account_scope
from plugin_enumeration import EnumerationUnavailable, install_enumeration


class OwnerUidTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())

        class Profile(GsBind):
            path = Path(folder) / "bindings.sqlite3"

        self.model = Profile

    async def test_list_operations_preserve_existing_owner_and_profile(self):
        # Create a pre-existing record with the original binding port.
        with binding_scope("a", "bot", True, writable=True):
            await self.model.insert_uid("a", "bot", "000000731", lenth_limit=9)
        with binding_scope("a", "bot", True, uid_write=True):
            self.assertEqual(await self.model.insert_uid("a", "bot", "000000731"), -2)
            self.assertEqual(await self.model.insert_uid("a", "bot", "000000732"), 0)
            self.assertEqual(await self.model.switch_uid_by_game("a", "bot"), 0)
            self.assertEqual(await self.model.delete_uid("a", "bot", "000000731"), 0)
            self.assertEqual(await self.model.update_data("a", "bot", group_id=""), 0)
        with binding_scope("a", "bot", True):
            self.assertEqual(await self.model.get_uid_by_game("a", "bot"), "000000732")
        with binding_scope("b", "bot", True, uid_write=True):
            self.assertIsNone(await self.model.get_uid_by_game("b", "bot"))
            with self.assertRaises(NotImplementedError):
                await self.model.get_uid_by_game("a", "bot")

    async def test_uid_action_cannot_supply_credentials_enumerate_or_set_group(self):
        models = SimpleNamespace(GsBind=self.model, GsUser=GsUser)
        authority = install_enumeration(
            models, admins={("a", "bot")}, read_rows=lambda _: ()
        )
        with binding_scope("a", "bot", True, uid_write=True):
            for uid in ("x", "０１２３４５", "0" * 33):
                self.assertEqual(
                    await self.model.insert_uid("a", "bot", uid, is_digit=False), -3
                )
            with self.assertRaises(NotImplementedError):
                await self.model.insert_uid("a", "bot", "000000731", group_id="room")
            with self.assertRaises(NotImplementedError):
                await self.model.update_data("a", "bot", group_id="room")
            with self.assertRaises(PermissionError), account_scope("gs", object()):
                self.fail("unexpected credential capability")
            with self.assertRaises(EnumerationUnavailable), authority.delivery():
                self.fail("unexpected inventory capability")
            with self.assertRaises(NotImplementedError):
                await asyncio.create_task(
                    self.model.insert_uid("a", "bot", "000000731")
                )
            copied = copy_context()
        with self.assertRaises(NotImplementedError):
            await asyncio.create_task(
                self.model.insert_uid("a", "bot", "000000731"), context=copied
            )
        with binding_scope("a", "bot", True, uid_read=False):
            with self.assertRaises(NotImplementedError):
                await self.model.get_uid_by_game("a", "bot")
