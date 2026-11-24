"""One in-process ASGI application per plugin generation; the worker opens no listener."""

from threading import RLock


def install_app_life(package):
    module = package("gsuid_core.app_life")
    alias = package("gsuid_core.web_app")
    lock = RLock()

    def resolve(name):
        if name != "app":
            raise AttributeError(name)
        # Profiles that do not use HTTP routes need no FastAPI dependency.
        from fastapi import FastAPI
        from starlette.middleware.gzip import GZipMiddleware

        with lock:
            if "app" not in vars(module):
                app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
                app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=6)
                module.app = alias.app = app
            return module.app

    module.__getattr__ = alias.__getattr__ = resolve
