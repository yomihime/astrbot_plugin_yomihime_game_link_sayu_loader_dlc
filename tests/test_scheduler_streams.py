import asyncio
from builtins import anext
from contextvars import copy_context
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import MagicMock

from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    EVENT_JOB_MISSED,
    EVENT_SCHEDULER_SHUTDOWN,
)
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
from plugin_bindings import _current, binding_scope
from plugin_scheduler import GenerationScheduler
from port_errors import Unsupported
from resource_httpx import _streams, static_httpx, stream_scope
from service_settings import _grant, management_scope

URL = "https://mirror.example.org/Fixture/speedtest"
PROFILE = SimpleNamespace(
    PACKAGE="Fixture",
    STATIC_RESOURCE_URLS=(),
    PUBLIC_IMAGE_ORIGINS=(),
    PUBLIC_BULK_MIRRORS=("https://mirror.example.org",),
    STATIC_RESOURCE_DIRECTORIES=(),
    STATIC_STREAM_OBJECTS=((URL, "application/octet-stream", 4),),
)


class Channel:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.parts = asyncio.Queue()
        self.parts.put_nowait((200, [], b""))
        self.parts.put_nowait(("chunk", [], b"ab"))
        self.cancelled_on = None

    async def next(self):
        return await self.parts.get()

    def cancel(self):
        self.cancelled_on = threading.get_ident()
        self.parts.put_nowait(("unavailable", [], b""))


class JobStreamTests(IsolatedAsyncioTestCase):
    async def test_cancelled_async_job_cannot_open_again_before_exiting(self):
        scheduler = GenerationScheduler()
        opened, denied = asyncio.Event(), asyncio.Event()
        reader = MagicMock(stream=MagicMock(side_effect=lambda *_: Channel()))

        async def job():
            async with httpx.AsyncClient() as client:
                async with client.stream("GET", URL) as response:
                    await anext(response.aiter_bytes())
                    opened.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        with self.assertRaises(Unsupported):
                            await client.get(URL)
                        denied.set()
                        raise

        with static_httpx(PROFILE, reader):
            scheduler.start()
            try:
                scheduler.add_job(job, "date")
                await asyncio.wait_for(opened.wait(), 5)
            finally:
                scheduler.shutdown(wait=False)
            await asyncio.wait_for(denied.wait(), 5)
            await asyncio.sleep(0)
        self.assertEqual(reader.stream.call_count, 1)
        self.assertTrue(reader.static_httpx[-1]["closed"])

    async def test_actual_runs_get_independent_empty_scopes_and_errors_close_streams(
        self,
    ):
        scheduler = GenerationScheduler()
        observed, tasks, events = [], [], []
        done = asyncio.Event()
        channels = []

        def open_stream(*unused):
            channel = Channel()
            channels.append(channel)
            return channel

        async def actual(fail):
            self.assertIsNone(_grant.get())
            self.assertIsNone(_current.get())
            observed.append(_streams.get())
            client = httpx.AsyncClient()
            response = await client.send(client.build_request("GET", URL), stream=True)
            self.assertEqual(await anext(response.aiter_bytes()), b"ab")
            tasks.append((copy_context(), client))
            if fail:
                raise RuntimeError("synthetic scheduled failure")
            return len(observed)

        def event(item):
            events.append(item)
            if len(events) == 5:
                done.set()

        mask = EVENT_JOB_ERROR | EVENT_JOB_EXECUTED | EVENT_JOB_MISSED
        scheduler.add_listener(event, mask)
        reader = MagicMock(stream=MagicMock(side_effect=open_stream))
        anchor = datetime.now(timezone.utc)
        with (
            static_httpx(PROFILE, reader),
            management_scope(writable=True),
            binding_scope("actor", "bot", True),
            stream_scope(),
        ):
            owner = _streams.get()
            scheduler.start(paused=True)
            try:
                begin = anchor - timedelta(minutes=3)
                scheduler.add_job(
                    partial(actual, False),
                    "interval",
                    id="backlog",
                    seconds=60,
                    start_date=begin,
                    next_run_time=begin,
                    end_date=anchor - timedelta(seconds=60),
                    coalesce=False,
                    misfire_grace_time=None,
                )
                scheduler.add_job(actual, "date", args=[True], id="error")
                scheduler.add_job(
                    actual,
                    "date",
                    id="missed",
                    args=[False],
                    run_date=begin,
                    misfire_grace_time=1,
                )
                scheduler.resume()
                await asyncio.wait_for(done.wait(), 5)
                self.assertEqual(
                    [e.scheduled_run_time for e in events if e.job_id == "backlog"],
                    [begin + timedelta(minutes=n) for n in range(3)],
                )
                self.assertCountEqual(
                    [e.code for e in events],
                    [EVENT_JOB_EXECUTED] * 3 + [EVENT_JOB_ERROR, EVENT_JOB_MISSED],
                )
                self.assertEqual(len(set(observed)), 4)
                self.assertNotIn(owner, observed)
                self.assertTrue(owner.active)
                self.assertTrue(all(not lease.active for lease in observed))
                self.assertTrue(
                    all(c.cancelled_on == threading.get_ident() for c in channels)
                )
                for context, client in tasks:
                    with self.assertRaises(Unsupported):
                        await asyncio.create_task(client.get(URL), context=context)
                    await client.aclose()
                self.assertEqual(reader.stream.call_count, 4)
            finally:
                scheduler.shutdown(wait=False)
                await asyncio.sleep(0)

    async def test_retirement_wakes_private_sync_loop_without_stopping_the_thread(self):
        scheduler = GenerationScheduler()
        loop = asyncio.get_running_loop()
        ready, finished, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
        hold = threading.Event()
        observed = {}
        channels = []

        def open_stream(*unused):
            channels.append(Channel())
            return channels[-1]

        async def consume():
            observed["lease"] = _streams.get()
            observed["owner"] = _current.get()
            observed["management"] = _grant.get()
            async with httpx.AsyncClient() as client:
                async with client.stream("GET", URL) as response:
                    body = response.aiter_bytes()
                    self.assertEqual(await anext(body), b"ab")
                    loop.call_soon_threadsafe(ready.set)
                    with self.assertRaises((Unsupported, ConnectionError)):
                        await anext(body)
                with self.assertRaises(Unsupported):
                    await client.get(URL)

        def sync_job():
            try:
                observed["thread"] = threading.get_ident()
                asyncio.run(consume())
                observed["complete"] = hold.wait(5)
            finally:
                loop.call_soon_threadsafe(finished.set)

        scheduler.add_listener(lambda _: stopped.set(), EVENT_SCHEDULER_SHUTDOWN)
        with static_httpx(
            PROFILE, MagicMock(stream=MagicMock(side_effect=open_stream))
        ):
            with (
                management_scope(writable=True),
                binding_scope("actor", "bot", True),
                stream_scope(),
            ):
                scheduler.start()
                job = scheduler.add_job(sync_job, "date")
                self.assertIs(job.func, sync_job)
                try:
                    await asyncio.wait_for(ready.wait(), 5)
                    scheduler.shutdown(wait=False)
                    await asyncio.wait_for(stopped.wait(), 5)
                    self.assertFalse(observed["lease"].active)
                finally:
                    hold.set()
                    if scheduler.running:
                        scheduler.shutdown(wait=False)
                await asyncio.wait_for(finished.wait(), 5)
        self.assertTrue(observed["complete"])
        self.assertIsNone(observed["owner"])
        self.assertIsNone(observed["management"])
        self.assertNotEqual(observed["thread"], threading.get_ident())
        self.assertEqual(channels[0].cancelled_on, observed["thread"])
        self.assertEqual(len(channels), 1)
