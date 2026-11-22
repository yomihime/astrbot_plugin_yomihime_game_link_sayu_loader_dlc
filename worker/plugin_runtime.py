"""Import a pinned plugin package and own its hooks and scheduled jobs for one generation."""

import asyncio
import importlib
import importlib.util
import inspect
import sys
from collections import defaultdict
from pathlib import Path

from sayu_compat import package
import plugin_bindings
import plugin_errors


class PluginRuntime:
    def __init__(self, data):
        self.data = data
        self.before_hooks = defaultdict(list)
        self.start_hooks = defaultdict(list)
        self.shutdown_hooks = defaultdict(list)
        from apscheduler.events import EVENT_JOB_ERROR
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        class Scheduler(AsyncIOScheduler):
            def scheduled_job(self, *args, **kwargs):
                kwargs.pop("replace_existing", None)
                return super().scheduled_job(*args, **kwargs)

        self.scheduler = Scheduler(
            timezone="Asia/Shanghai",
            job_defaults={"misfire_grace_time": 90, "coalesce": True},
        )
        self.scheduler.add_listener(self.job_error, EVENT_JOB_ERROR)
        self.failed_jobs = set()

    def install(self, source):
        plugin_bindings.install(package, self.data)
        sys.modules["gsuid_core.utils.error_reply"] = plugin_errors
        package("plugins", source)
        server = package("gsuid_core.server")
        server.on_core_start_before = self.hook(self.before_hooks)
        server.on_core_start = self.hook(self.start_hooks)
        server.on_core_shutdown = self.hook(self.shutdown_hooks)
        aps = package("gsuid_core.aps")
        aps.scheduler = self.scheduler
        aps.get_all_aps_job = self.scheduler.get_jobs

    def job_error(self, event):
        self.failed_jobs.add(event.job_id)

    @staticmethod
    def hook(hooks):
        def decorate(func=None, /, *, priority=0):
            def register(callback):
                hooks[priority].append(callback)
                return callback

            return register if func is None else register(func)

        return decorate

    @staticmethod
    async def run_hooks(hooks):
        for priority in sorted(hooks):
            await asyncio.gather(
                *(
                    fn() if inspect.iscoroutinefunction(fn) else asyncio.to_thread(fn)
                    for fn in hooks[priority]
                )
            )

    def import_package(self, source, plugin):
        if not plugin.isidentifier():
            raise ValueError("invalid package name")
        self.install(source)
        root = Path(source) / plugin
        namespace = f"plugins.{plugin}"
        sys.path.append(str(source))
        nested = (root / plugin).is_dir() or (root / "__nest__.py").is_file()
        full = (root / "__full__.py").is_file()
        if nested or full:
            package(namespace, root)
            directory = root if full else root / plugin
            sys.path.append(str(directory.parent))
            sys.path.append(str(directory))
            if full:
                # Sayu's full layout names its root plugins.<name>.<name>
                # while service packages sit directly under plugins.<name>.
                spec = importlib.util.spec_from_file_location(
                    f"{namespace}.{plugin}", directory / "__init__.py"
                )
                if spec is None or spec.loader is None:
                    raise ImportError("missing package initializer")
                loaded = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = loaded
                spec.loader.exec_module(loaded)
            else:
                namespace = f"{namespace}.{plugin}"
                importlib.import_module(namespace)
            for child in sorted(directory.iterdir()):
                if child.is_dir() and (child / "__init__.py").is_file():
                    importlib.import_module(f"{namespace}.{child.name}")
        elif (root / "__init__.py").is_file():
            importlib.import_module(namespace)
        else:
            raise ImportError("missing package initializer")

    async def start(self, source, plugin):
        self.import_package(source, plugin)
        await self.run_hooks(self.before_hooks)
        await self.run_hooks(self.start_hooks)
        self.scheduler.start()

    async def stop(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            # AsyncIOScheduler schedules shutdown on the event loop.
            await asyncio.sleep(0)
        await self.run_hooks(self.shutdown_hooks)
