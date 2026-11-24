import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
import plugin_mys as mys
from plugin_bindings import binding_scope


class MysAccountTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.uid = "100000001"
        self.authorize = Mock(return_value={"DS": "issuer-fixture"})
        self.account = mys.Account(self.uid, "session-fixture", self.authorize)
        self.endpoint = mys.ENDPOINTS["DAILY_NOTE"]
        self.api = mys.MysApi()

    async def test_private_owner_scope_required_for_provisioning(self):
        for actor, adapter, private, writable in (
            ("", "adapter", True, True),
            ("actor", "", True, True),
            ("actor", "adapter", False, True),
            ("actor", "adapter", True, False),
        ):
            with self.subTest(
                actor=actor, adapter=adapter, private=private, writable=writable
            ):
                with (
                    binding_scope(actor, adapter, private, writable=writable),
                    self.assertRaises(PermissionError),
                    mys.account_scope("gs", self.account),
                ):
                    self.fail("unsupported scope was granted")
        self.assertEqual(await self.api.get_daily_data(self.uid), -51)
        self.assertEqual(
            await self.api.endpoint_request(
                self.endpoint, self.uid, cookie=self.account.cookie
            ),
            -51,
        )
        self.authorize.assert_not_called()

    async def test_lease_and_cache_are_isolated_from_other_actors_and_late_tasks(self):
        async def wait_and_read(event):
            await event.wait()
            return await self.api.get_ck(self.uid)

        event = asyncio.Event()
        with binding_scope("actor", "adapter", True, writable=True):
            with mys.account_scope("gs", self.account):
                late = asyncio.create_task(wait_and_read(event))
                self.assertEqual(await self.api.get_ck(self.uid), self.account.cookie)
                self.assertIsNone(await self.api.get_ck(self.uid, game_name="sr"))
                with patch.object(
                    self.api, "_record", new=AsyncMock(return_value={"sample": 1})
                ) as record:
                    self.assertEqual(await self.api.get_info(self.uid), {"sample": 1})
                    self.assertEqual(await self.api.get_info(self.uid), {"sample": 1})
                    record.assert_awaited_once()
                    with binding_scope("other", "adapter", True, writable=True):
                        self.assertIsNone(await self.api.get_ck(self.uid))
                        self.assertEqual(await self.api.get_info(self.uid), -51)
            event.set()
            self.assertIsNone(await late)
            with mys.account_scope("gs", self.account):
                with patch.object(
                    self.api, "_record", new=AsyncMock(return_value={"sample": 2})
                ):
                    self.assertEqual(await self.api.get_info(self.uid), {"sample": 2})

    async def test_owner_record_routing_denials_never_open_http(self):
        with (
            binding_scope("actor", "adapter", True, writable=True),
            mys.account_scope("gs", self.account),
        ):
            for fields in (
                {"cookie": "foreign-session"},
                {"cookie_mode": "unknown"},
                {"ds_mode": "unknown"},
                {"params": {"role_id": self.uid, "uid": "100000002"}},
                {"params": {"role_id": self.uid, "server": "os_usa"}},
                {"params": {"sample": 1}},
            ):
                self.assertEqual(
                    await self.api.endpoint_request(self.endpoint, self.uid, **fields),
                    -51,
                )
            self.assertIsNone(await self.api.get_ck(self.uid, mode="unknown"))
            self.assertIsNone(await self.api.get_ck("100000002"))
            self.assertEqual(
                await self.api._mys_request(
                    self.endpoint.os,
                    header={"Cookie": self.account.cookie},
                    params={"uid": self.uid},
                ),
                -51,
            )
            for method, url in (
                ("GET", "https://example.invalid/"),
                ("POST", self.endpoint.cn),
            ):
                with self.assertRaises(NotImplementedError):
                    await self.api._mys_request(
                        url, method, {"Cookie": self.account.cookie}, {"uid": self.uid}
                    )
            self.authorize.assert_not_called()
            self.assertEqual(
                await self.api.endpoint_request(
                    self.endpoint,
                    self.uid,
                    params={"uid": self.uid},
                    header={"x-rpc-device_fp": "other"},
                ),
                -51,
            )
            self.authorize.assert_called_once()

    async def test_live_inherited_task_and_adapter_do_not_receive_lease(self):
        from plugin_mys_cache import cache_decorator

        @cache_decorator()
        async def cached_cookie(uid):
            return await self.api.get_ck(uid)

        async def child():
            self.assertIsNone(await self.api.get_ck(self.uid))
            self.assertIsNone(await cached_cookie(self.uid))
            self.assertEqual(await self.api.get_daily_data(self.uid), -51)
            with (
                self.assertRaises(PermissionError),
                mys.account_scope("gs", self.account),
            ):
                self.fail("a child task issued a grant")

        with (
            binding_scope("actor", "adapter", True, writable=True),
            mys.account_scope("gs", self.account),
        ):
            self.assertEqual(await cached_cookie(self.uid), self.account.cookie)
            await asyncio.create_task(child())
            for actor, adapter, private in (
                ("actor", "different", True),
                ("actor", "adapter", False),
                ("other", "adapter", True),
            ):
                with binding_scope(actor, adapter, private, writable=True):
                    self.assertIsNone(await self.api.get_ck(self.uid))
                    self.assertEqual(await self.api.get_daily_data(self.uid), -51)
            self.assertEqual(await self.api.get_ck(self.uid), self.account.cookie)
        self.authorize.assert_not_called()

    async def test_missing_issuer_or_empty_signature_stops_before_http(self):
        with binding_scope("actor", "adapter", True, writable=True):
            unsigned = mys.Account(self.uid, "test")
            with (
                mys.account_scope("gs", unsigned),
                self.assertRaises(NotImplementedError),
            ):
                await self.api.get_daily_data(self.uid)
            self.authorize.return_value = {}
            with mys.account_scope("gs", self.account):
                self.assertEqual(await self.api.get_daily_data(self.uid), -51)

    async def test_cancellation_and_parallel_owners_at_transport(self):
        import httpx2

        arrivals = {}
        sent = []
        both_arrived = asyncio.Event()
        release = asyncio.Event()

        async def respond(req):
            cookie = req.headers["cookie"]
            sent.append(cookie)
            arrivals[cookie] = mys._current_account.get()
            if len(arrivals) == 2:
                both_arrived.set()
            await release.wait()
            return httpx2.Response(200, json={"retcode": 0, "data": {"owner": cookie}})

        async def deliver(actor):
            account = mys.Account(self.uid, actor, self.authorize)
            with (
                binding_scope(actor, "adapter", True, writable=True),
                mys.account_scope("gs", account),
            ):
                return await self.api.get_info(self.uid)

        client = httpx2.AsyncClient
        with patch.object(
            httpx2,
            "AsyncClient",
            lambda **kw: client(transport=httpx2.MockTransport(respond), **kw),
        ):
            first = asyncio.create_task(deliver("owner-a"))
            second = asyncio.create_task(deliver("owner-b"))
            try:
                await asyncio.wait_for(both_arrived.wait(), 5)
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first
                self.assertFalse(arrivals["owner-a"].active)
                self.assertFalse(arrivals["owner-a"].owner.active)
                self.assertFalse(arrivals["owner-a"].cache)
                self.assertTrue(arrivals["owner-b"].active)
                release.set()
                self.assertEqual(await second, {"owner": "owner-b"})
                self.assertFalse(arrivals["owner-b"].active)
                self.assertFalse(arrivals["owner-b"].cache)
            finally:
                first.cancel()
                second.cancel()
                await asyncio.gather(first, second, return_exceptions=True)
        self.assertCountEqual(sent, ["owner-a", "owner-b"])

    async def test_pinned_endpoint_and_servers(self):
        ep = mys.ApiEndpoint(os="https://example.invalid/{x}", name="sample")
        self.assertTrue(ep.has_os)
        self.assertFalse(ep.has_cn)
        self.assertEqual(str(ep), ep.get(True))
        with self.assertRaisesRegex(ValueError, "endpoint sample has no cn URL"):
            ep.format(x="a")
        self.assertTrue(self.api.check_os("700000001", "sr"))
        self.assertEqual(
            self.api.get_server_id("700000001", "sr"), "prod_official_euro"
        )
        self.assertEqual(self.api.get_server_id("500000001"), "cn_qd01")
