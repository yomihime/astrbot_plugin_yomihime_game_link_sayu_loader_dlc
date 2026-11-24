"""Import a pinned plugin package and own its hooks and scheduled jobs for one generation."""

import asyncio
import importlib
import importlib.util
import inspect
import sys
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path

from sayu_compat import package
import plugin_bindings
import plugin_errors
from plugin_mys import install_mys
from plugin_enumeration import install_enumeration
from plugin_sign import install_sign
from plugin_gss import install_gss
from plugin_meta import install_meta, read_meta_plugin_info
from plugin_scheduler import GenerationScheduler, install_scheduler
from plugin_backup import install_backup
from plugin_mys_cache import install_cache
from port_errors import Unsupported


@dataclass(eq=False)
class Hook:
    priority: int
    func: object

    def __hash__(self):
        return hash(self.func)

    def __eq__(self, other):
        if isinstance(other, Hook):
            return self.func is other.func
        return NotImplemented

    def __lt__(self, other):
        return self.priority < other.priority


def module_entries(source, plugin):
    """Resolve the entrypoints in native registration order inside one profile."""
    if not plugin.isidentifier() or plugin.startswith("_"):
        raise ValueError("invalid package name")
    root = Path(source) / plugin
    namespace = f"plugins.{plugin}"
    single = root.with_suffix(".py")
    if not root.exists() and single.is_file():
        return [(namespace, single)]
    full = (root / "__full__.py").is_file()
    nested = (root / "__nest__.py").is_file() or (root / plugin).is_dir()
    if not full and not nested:
        if (root / "__init__.py").is_file():
            return [(namespace, root / "__init__.py")]
        raise ImportError(f"missing package initializer for {plugin}")
    directory = root if full else root / plugin
    primary = f"{namespace}.{plugin}"
    entries = []
    if (directory / "__init__.py").is_file():
        entries.append((primary, directory / "__init__.py"))
    for child in directory.iterdir():
        init = child / "__init__.py"
        if init.is_file():
            entries.append((f"{namespace if full else primary}.{child.name}", init))
    return entries


def load_entry(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"no importer for {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


class PluginRuntime:
    def __init__(self, data, *, on_import_error=None):
        self.data = data
        self.modules = {}
        self.on_import_error = on_import_error
        self.before_hooks = set()
        self.start_hooks = set()
        self.shutdown_hooks = set()
        self.hook_errors = {}
        from apscheduler.events import EVENT_JOB_ERROR

        self.scheduler = GenerationScheduler()
        self.scheduler.add_listener(self.job_error, EVENT_JOB_ERROR)
        self.failed_jobs = set()

    def install(self, source, plugin):
        from gsuid_core.pool import shutdown_pools
        from plugin_orm import install_orm
        from plugin_admin import install_admin
        from plugin_subscribe import install_subscribe

        plugin_bindings.install(package, self.data)
        install_mys(package)
        install_sign(package)
        install_cache(package)
        install_backup(package, self.data, plugin)
        self.orm = install_orm(package, self.data, plugin)
        self.enumeration = install_enumeration(sys.modules["gsuid_core.utils.database.models"])
        install_subscribe(package, self.orm)
        install_admin(package, source, plugin)
        self.plugin = plugin
        self.shutdown_pools = shutdown_pools
        sys.modules["gsuid_core.utils.error_reply"] = plugin_errors
        package("plugins", source)
        server = package("gsuid_core.server")
        self.meta = install_meta(package("gsuid_core.meta_plugins"), server, plugin)
        self.adapters = install_gss(package, server)
        for name, hooks in (
            ("start_before", self.before_hooks),
            ("start", self.start_hooks),
            ("shutdown", self.shutdown_hooks),
        ):
            setattr(server, "core_" + name + "_def", hooks)
            setattr(server, "on_core_" + name, self.hook(hooks))
            setattr(server, "core_" + name + "_execute", self.manage_hooks(name, hooks))
        self.aps = install_scheduler(package("gsuid_core.aps"), self.scheduler)

    def job_error(self, event):
        self.failed_jobs.add(event.job_id)

    @staticmethod
    def hook(hooks):
        def decorate(func=None, /, *, priority=0):
            def register(callback):
                hooks.add(Hook(priority, callback))
                return callback

            return register if func is None else register(func)

        return decorate

    def manage_hooks(self, name, hooks):
        async def execute():
            from service_settings import _grant

            grant = _grant.get()
            if grant is None or not grant.writable:
                raise Unsupported("lifecycle callbacks require host management")
            await self.run_hooks(name, hooks)

        return execute

    async def run_hooks(self, name, hooks):
        from gsuid_core.logger import logger

        try:
            for _, group in groupby(sorted(hooks), key=lambda hook: hook.priority):
                await asyncio.gather(
                    *(
                        hook.func()
                        if inspect.iscoroutinefunction(hook.func)
                        else asyncio.to_thread(hook.func)
                        for hook in group
                    )
                )
        except Exception as exc:
            self.hook_errors[name] = type(exc).__name__
            logger.exception(exc)

    def import_package(self, source, plugin):
        self.install(source, plugin)
        self.load(source, plugin)

    def load(self, source, plugin):
        entries = module_entries(source, plugin)
        root = Path(source) / plugin
        if root.is_dir():
            from gsuid_core.i18n import discover_and_register_plugin_locales

            discover_and_register_plugin_locales(root, plugin)
        namespace = f"plugins.{plugin}"
        if any(name.startswith(namespace + ".") for name, _ in entries):
            package(namespace, root)
        for directory in (source, *(path.parent.parent for _, path in entries)):
            if str(directory) not in sys.path:
                sys.path.append(str(directory))
        for name, path in entries:
            try:
                load_entry(name, path)
            except Exception as exc:
                # Native GsServer isolates errors at the discovered module boundary.
                # Keep every failure visible without exporting plugin error text.
                self.modules[name] = type(exc).__name__
                if self.on_import_error is not None:
                    self.on_import_error(name, exc)
            else:
                self.modules[name] = None
        self.adapters.server.is_load = True
        info = read_meta_plugin_info(root)
        if info and not self.meta.bind_meta_plugin_facade(
            info.provides, plugin, self.modules
        ):
            from gsuid_core.logger import logger

            logger.warning("Infrastructure plugin has no loaded API facade")
        from sayu_compat import flush_loaded_service_settings

        flush_loaded_service_settings()

    async def start(self, source, plugin):
        self.install(source, plugin)
        async with self.orm.startup():
            self.load(source, plugin)
            self.aps.remove_repeat_job()
            await self.orm.migrate()
            await self.run_hooks("start_before", self.before_hooks)
            await self.run_hooks("start", self.start_hooks)
        self.scheduler.start()

    async def stop(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            # AsyncIOScheduler schedules shutdown on the event loop.
            await asyncio.sleep(0)
        try:
            await self.run_hooks("shutdown", self.shutdown_hooks)
        finally:
            self.shutdown_pools()
            self.adapters.server.is_load = False
            self.meta.unregister_meta_plugin(self.plugin)
            from gsuid_core.i18n import unregister_plugin_locales

            unregister_plugin_locales(self.plugin)
