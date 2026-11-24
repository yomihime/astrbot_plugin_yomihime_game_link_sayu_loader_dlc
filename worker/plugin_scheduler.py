"""One APScheduler and in-memory queue per plugin generation."""

from contextvars import Context
from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from port_errors import Unsupported


class GenerationScheduler(AsyncIOScheduler):
    def __init__(self):
        self._sealed = False
        super().__init__(
            timezone="Asia/Shanghai",
            jobstores={"default": MemoryJobStore()},
            executors={"default": AsyncIOExecutor()},
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
