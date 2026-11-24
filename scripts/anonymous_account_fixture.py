"""Offline SDK boundary for the unchanged Waves account handler in the package contract."""

import ast
import asyncio
from contextlib import contextmanager
from hashlib import sha256
from importlib import import_module
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch
from zipfile import ZipFile


class OfflineReads:
    def __init__(self, core, root, hits, *, digest=None, worker_hash=None):
        manifest = "packages/sayu_loader/yomihime.manifest.json"
        if root.is_dir():
            data = (root / manifest).read_bytes()
        else:
            if sha256(root.read_bytes()).hexdigest() != digest:
                raise ValueError("frozen host package digest mismatch")
            with ZipFile(root) as z:
                data = z.read(manifest)
                bindings = ast.literal_eval(
                    z.read("packages/sayu_loader/sayu/runtime.py")
                    .decode()
                    .split("=", 1)[1]
                    .strip()
                )
                if bindings["waves"][2] != worker_hash:
                    raise ValueError(
                        "worker does not belong to the frozen host package"
                    )
        sys.path[:0] = [str(core.parent), str(root / "packages")]

        def load(path):
            return import_module(core.name + "." + path)

        self.sdk = load("yomihime_game_link_sdk")
        sys.modules["sayu_loader._api_v2"] = self.sdk
        self.bridge = import_module("sayu_loader.sayu.anonymous_http").AnonymousReads
        protocol = load("extensions.hot_update_protocol")
        envelope = protocol.parse_package_manifest(
            data,
            host_install=True,
        )
        manifest = load("extensions.disk_manifest").parse_manifest(
            protocol.b05_manifest_bytes(envelope)
        )
        declarations = next(
            m.sources for m in manifest.modules if m.module_id == "sayuww"
        )
        self.module_id = manifest.global_module_id("sayuww")
        rules = import_module("sayu_loader.sayu.anonymous_sources").SOURCES["sayuww"]
        assert {src.source_id: src.host for src in declarations} == {
            key: rule[0] for key, rule in rules.items()
        }
        assert all(
            src.max_redirects == 0
            and src.credential_ref is None
            and not src.authorization_only
            for src in declarations
        )
        self.count = 0
        self.calls = []
        self.origin = {"sayu_kuro_ip": (200, b"192.0.2.1")}

        async def transport(request):
            assert (
                request.method == "GET" and not request.headers and request.body is None
            )
            assert request.credential_ref is None
            hits.append([request.method, request.host, request.path])
            self.count += 1
            self.calls.append((request.source_id, request.path, request.query))
            response = self.origin.get(request.source_id, (503, b"fixture unavailable"))
            return self.sdk.HttpResponse(response[0], {}, response[1])

        self.http = load("infrastructure.http").SourceHttpService(
            declarations, transport, module_id=self.module_id
        )
        self.bound = load("services.module_services")._BoundHttp

    @contextmanager
    def invocation(self):
        from anonymous_http import read_scope

        nonce = os.urandom(16).hex()
        active = True

        def check_sync():
            if not active:
                raise self.sdk.AccessDenied()

        async def check():
            check_sync()

        host = self.bridge("sayuww", nonce, self.bound(self.http, check_sync, check))
        read_fd, write_fd = os.pipe()
        pending = []
        with (
            os.fdopen(read_fd, "rb") as reader,
            os.fdopen(write_fd, "wb", buffering=0) as writer,
        ):

            async def receive(frame):
                try:
                    reply = await host.reply(frame)
                except BaseException:
                    writer.write(b"{}\n")
                    raise
                writer.write(json.dumps(reply).encode("ascii") + b"\n")

            def submit(frame):
                pending.append(asyncio.create_task(receive(frame)))

            try:
                with (
                    patch.object(sys, "stdin", SimpleNamespace(buffer=reader)),
                    read_scope(nonce, submit),
                ):
                    yield
            finally:
                active = False
                for task in pending:
                    if task.done():
                        task.result()
                    else:
                        task.cancel()

    async def check_fallbacks(self):
        import httpx
        from plugins.XutheringWavesUID.XutheringWavesUID.utils.util import get_public_ip
        from port_errors import Unsupported

        kuro, ipify, httpbin = ("sayu_kuro_ip", "sayu_ipify", "sayu_httpbin")
        steps = (
            ({ipify: (200, b'{"ip":"192.0.2.2"}')}, "192.0.2.2", (kuro, ipify)),
            (
                {httpbin: (200, b'{"origin":"192.0.2.3"}')},
                "192.0.2.3",
                (kuro, ipify, httpbin),
            ),
            ({}, "127.127.127.127", (kuro, ipify, httpbin)),
        )
        for index, (origins, expected, attempts) in enumerate(steps):
            self.calls.clear()
            self.origin = origins
            with self.invocation():
                # The unchanged function caches by argument for 24 hours.
                fallback = f"192.0.2.{90 + index}"
                actual = await get_public_ip(fallback)
                if not origins:
                    expected = fallback
                assert actual == expected, (actual, expected, self.calls)
            assert [c[0] for c in self.calls] == list(attempts)
            for source, path, query in self.calls:
                assert (path, query) == {
                    kuro: ("/event/ip", ()),
                    ipify: ("/", (("format", "json"),)),
                    httpbin: ("/ip", ()),
                }[source]
        self.origin = {
            kuro: (200, b"<html>error</html>"),
            ipify: (200, b'{"ip": false}'),
            httpbin: (200, b'{"origin":"192.0.2.3, 2001:db8::9"}'),
        }
        with self.invocation():
            assert await get_public_ip("192.0.2.93") == "192.0.2.3, 2001:db8::9"
        self.origin = {
            kuro: (200, b"x" * 5000),
            ipify: (200, b'{"ip":"192.0.2.1","ip":"192.0.2.2"}'),
        }
        with self.invocation():
            assert await get_public_ip("192.0.2.94") == "192.0.2.94"
        before = self.count
        url = "https://api.ipify.org/?format=json"
        async with httpx.AsyncClient() as client:
            with self.invocation():
                for method, address, kwargs in (
                    ("POST", url, {}),
                    ("GET", url + "&uid=private", {}),
                    ("GET", "https://api.ipify.org/other?format=json", {}),
                    ("GET", url, {"headers": {"Authorization": "fixture"}}),
                    ("GET", url, {"headers": {"Cookie": "fixture=1"}}),
                    ("GET", url, {"content": b"fixture"}),
                ):
                    try:
                        await client.request(method, address, **kwargs)
                    except (Unsupported, httpx.RequestError):
                        pass
                    else:
                        raise AssertionError(
                            "non-anonymous or undeclared request accepted"
                        )
                child = asyncio.create_task(client.get(url))
                try:
                    await child
                except Unsupported:
                    pass
                else:
                    raise AssertionError("copied task inherited anonymous read scope")
            try:
                await client.get(url)
            except Unsupported:
                pass
            else:
                raise AssertionError("request accepted after invocation")
        assert before == self.count
        return {
            "fixed_fallbacks": len(steps),
            "rejected_body_scenarios": 2,
            "denied_without_transport": 8,
        }
