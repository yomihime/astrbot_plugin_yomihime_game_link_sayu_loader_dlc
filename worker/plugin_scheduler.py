"""One APScheduler and in-memory queue per plugin generation."""

import asyncio
from contextlib import contextmanager
from contextvars import Context
from threading import RLock
from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.util import iscoroutinefunction_partial
from port_errors import Unsupported
from resource_httpx import _streams, stream_scope


class _JobStreams:
    """Close each response on the loop that created it, including sync jobs' private loops."""

    def __init__(self, lease):
        self.lease = lease
        self.bodies = {}
        self.lock = RLock()

    def add(self, body):
        loop = asyncio.get_running_loop()
        with self.lock:
            if self.lease.active:
                self.bodies[body] = loop
            else:
                # A different thread retired the job during response construction.
                loop.call_soon(body.close)

    def discard(self, body):
        with self.lock:
            self.bodies.pop(body, None)

    def __iter__(self):
        with self.lock:
            return iter(tuple(self.bodies))

    def retire(self):
        with self.lock:
            self.lease.active = False
            bodies, self.bodies = self.bodies, {}
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        for body, loop in bodies.items():
            if loop is current or not loop.is_running():
                body.close()
            else:
                try:
                    loop.call_soon_threadsafe(body.close)
                except RuntimeError:
                    if not loop.is_closed():
                        raise
                    body.close()


class _SubmittedJob:
    """Only the executor sees the wrapper. Public jobs retain the plugin's callable."""

    def __init__(self, job, scoped_func):
        self.original = job
        self.func = scoped_func

    def __getattr__(self, name):
        return getattr(self.original, name)

    def __str__(self):
        return str(self.original)


class _StreamExecutor(AsyncIOExecutor):
    def start(self, scheduler, alias):
        super().start(scheduler, alias)
        self.runs = set()

    @contextmanager
    def execution(self, generation):
        with stream_scope():
            lease = _streams.get()
            streams = lease.live = _JobStreams(lease)
            with self._lock:
                if generation is None or self.runs is not generation:
                    raise asyncio.CancelledError("scheduled generation retired")
                generation.add(streams)
            try:
                yield
            finally:
                try:
                    streams.retire()
                finally:
                    with self._lock:
                        generation.discard(streams)

    def _do_submit_job(self, job, run_times):
        generation = self.runs
        if iscoroutinefunction_partial(job.func):

            async def invoke(*args, **kwargs):
                with self.execution(generation):
                    return await job.func(*args, **kwargs)

            async def scoped(*args, **kwargs):
                return await asyncio.create_task(
                    invoke(*args, **kwargs), context=Context()
                )
        else:

            def invoke(*args, **kwargs):
                with self.execution(generation):
                    return job.func(*args, **kwargs)

            def scoped(*args, **kwargs):
                return Context().run(invoke, *args, **kwargs)

        # APScheduler handles misfires, coalescing, concurrency and completion.
        # It invokes the wrapper separately for every run time that actually executes.
        super()._do_submit_job(_SubmittedJob(job, scoped), run_times)

    def shutdown(self, wait=True):
        with self._lock:
            running, self.runs = tuple(self.runs), None
        for streams in running:
            streams.retire()
        super().shutdown(wait)


class GenerationScheduler(AsyncIOScheduler):
    def __init__(self):
        self._sealed = False
        super().__init__(
            timezone="Asia/Shanghai",
            jobstores={"default": MemoryJobStore()},
            executors={"default": _StreamExecutor()},
            job_defaults={"misfire_grace_time": 90, "coalesce": True},
        )
        self._sealed = True

    def _private_only(self):
        if self._sealed:
            raise Unsupported("the worker owns its in-memory scheduler configuration")

    def configure(self, *args, **kwargs):
        self._private_only()
        return super().configure(*args, **kwargs)

    def add_jobstore(self, *args, **kwargs):
        self._private_only()
        return super().add_jobstore(*args, **kwargs)

    def remove_jobstore(self, *args, **kwargs):
        self._private_only()
        return super().remove_jobstore(*args, **kwargs)

    def add_executor(self, *args, **kwargs):
        self._private_only()
        return super().add_executor(*args, **kwargs)

    def remove_executor(self, *args, **kwargs):
        self._private_only()
        return super().remove_executor(*args, **kwargs)

    def scheduled_job(self, *args, **kwargs):
        kwargs.pop("replace_existing", None)
        return super().scheduled_job(*args, **kwargs)

    def wakeup(self):
        # call_soon_threadsafe otherwise propagates the active chat's private
        # context through the scheduler timer to jobs that outlive the handler.
        return Context().run(super().wakeup)


def install_scheduler(module, scheduler):
    from plugin_scheduler_helpers import bind

    module.scheduler = scheduler
    for name, value in bind(scheduler).items():
        setattr(module, name, value)
    return module
