"""Thread decorators and shutdown for one plugin generation."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
import inspect

from port_errors import Unsupported


def install_pool(package):
    module = package("gsuid_core.pool")
    executor = ThreadPoolExecutor(max_workers=10)

    def to_thread(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            def invoke():
                if not inspect.iscoroutinefunction(func):
                    return func(*args, **kwargs)
                loop = asyncio.new_event_loop()
                try:
                    asyncio.set_event_loop(loop)
                    return loop.run_until_complete(func(*args, **kwargs))
                finally:
                    loop.close()

            # The native executor doesn't copy the caller's ContextVars. In
            # particular a private invocation or management grant stays on its task.
            return await asyncio.get_running_loop().run_in_executor(executor, invoke)

        return wrapper

    def to_process(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            raise Unsupported("plugin process pool requires an isolated child runtime")

        return wrapper

    def shutdown_pools():
        executor.shutdown(wait=False, cancel_futures=True)

    module.to_thread = module.run_in_thread_pool = to_thread
    module.to_process = to_process
    module.shutdown_pools = shutdown_pools
    return module
