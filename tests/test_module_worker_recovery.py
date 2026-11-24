"""Real worker subprocesses record requests before replying on the framed pipe."""

import asyncio
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
from zipfile import ZipFile


MODULE = Path(__file__).resolve().parents[1] / "packages/sayu_loader/sayu/module.py"
WORKER = """
import json,os,sys,time
from pathlib import Path
data=Path(sys.argv[2]);data.mkdir(exist_ok=True)
def record(label):
    with (data/'journal').open('a',encoding='utf8') as stream:
        stream.write(json.dumps([label,os.getpid()])+'\\n')
def send(obj):
    b=json.dumps(obj).encode()
    sys.stdout.buffer.write(len(b).to_bytes(4,'big')+b)
    sys.stdout.buffer.flush()
def gate(name):
    while (data/name).exists():
        time.sleep(.01)
record('boot')
gate('delay-start')
send({'ready':True})
for line in sys.stdin:
    request=json.loads(line)
    label=request['text']
    record(label)
    if label=='broker':
        import atexit,resource_broker
        broker=resource_broker.activate(Path(sys.argv[0]).resolve())
        broker.__enter__()
        atexit.register(broker.__exit__,None,None,None)
        while not Path('broker.pid').is_file():
            time.sleep(.01)
        (data/'broker-tree').write_text(json.dumps([os.getpid(),resource_broker.CLIENT.process.pid,int(Path('broker.pid').read_text())]))
    if label=='exit':os._exit(1)
    if label=='bad':
        sys.stdout.buffer.write(b'\\x00'*4);sys.stdout.buffer.flush()
        continue
    gate('delay-reply')
    send({'label':label,'parts':[{'text':label}],'pid':os.getpid(),'owner':request.get('owner_action',False),'prefix':sys.prefix})
"""


def adapter(binding):
    # Import the production adapter with only the SDK symbols resolved at import.
    package = ModuleType("recovery_fixture")
    sdk = package._api_v2 = ModuleType("recovery_fixture._api_v2")
    sdk.ServiceUnavailable = type("Unavailable", (RuntimeError,), {})
    sdk.AccessDenied = type("AccessDenied", (RuntimeError,), {})
    sdk.InvalidInvocation = type("InvalidInvocation", (RuntimeError,), {})
    sdk.Privacy = SimpleNamespace(PRIVATE="private", PUBLIC="public")
    sdk.ErrorCode = SimpleNamespace(NOT_FOUND=1, UNSUPPORTED=2, UPSTREAM_ERROR=3)
    sdk.ModuleHandlers = lambda capabilities, *_: SimpleNamespace(
        capabilities=capabilities
    )
    runtime = ModuleType("recovery_fixture.sayu.runtime")
    runtime.WORKERS = {"probe": binding}
    spec = importlib.util.spec_from_file_location(
        "recovery_fixture.sayu.module", MODULE
    )
    target = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules, {package.__name__: package, runtime.__name__: runtime}
    ):
        spec.loader.exec_module(target)
    return target


class WorkerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.data = root / "private"
        worker, source = root / "worker.pyz", root / "src.zip"
        with ZipFile(worker, "w") as output:
            output.writestr("__main__.py", WORKER)
            for name in ("resource_broker.py", "port_errors.py"):
                output.write(MODULE.parents[3] / "worker" / name, name)
            output.writestr(
                "game_profile.py",
                "import os\nfrom pathlib import Path\nPUBLIC_RESOURCES_ONLY=True\nPath('broker.tmp').write_text(str(os.getpid()))\nPath('broker.tmp').replace('broker.pid')\n",
            )
        with ZipFile(source, "w") as output:
            output.writestr("probe/__init__.py", "")
        self.adapter = adapter(
            (
                sys.executable,
                str(worker),
                hashlib.sha256(worker.read_bytes()).hexdigest(),
                str(source),
                hashlib.sha256(source.read_bytes()).hexdigest(),
                str(self.data),
            )
        )
        self.module = self.adapter.Module("probe", None)
        await self.module.start()
        self.addAsyncCleanup(self.module.stop)

    def records(self, label):
        rows = (self.data / "journal").read_text("utf8").splitlines()
        return [pid for item, pid in map(json.loads, rows) if item == label]

    async def wait_for(self, label, count=1):
        async with asyncio.timeout(5):
            while len(self.records(label)) < count:
                await asyncio.sleep(0.01)

    def request(self, label):
        return self.module.exchange({"text": label})

    async def test_command_uses_callers_deadline(self):
        from unittest.mock import AsyncMock

        sdk = self.adapter.ygl
        sdk.ResultStatus = SimpleNamespace(SUCCESS="success")
        sdk.CapabilityResult = lambda kind, status, doc, **kwargs: (status, doc)
        command = self.module.handlers().capabilities["query"]
        process = self.module.process
        gate = self.data / "delay-reply"
        gate.touch()
        loop = asyncio.get_running_loop()
        clock = loop.time
        with patch.object(command, "document", AsyncMock(return_value="delivered")):
            async with asyncio.timeout(60):
                pending = asyncio.create_task(command.invoke(None, {"text": "long"}))
                await self.wait_for("long")
                # Advance beyond the old 20-second module timer, keeping the
                # actual enclosing deadline and IPC exchange alive.
                with patch.object(loop, "time", lambda: clock() + 25):
                    await asyncio.sleep(0)
                    await asyncio.sleep(0)
                    gate.unlink()
                    self.assertEqual(await pending, ("success", "delivered"))
        self.assertEqual(self.records("long"), [process.pid])
        self.assertIs(self.module.process, process)

    @unittest.skipUnless(
        os.name == "nt", "Windows worker and venv launcher process handles"
    )
    async def test_force_retirement_reaps_real_broker_tree_and_keeps_venv(self):
        import _winapi
        from contextlib import ExitStack

        first = self.module.process
        hello = await self.request("identity")
        self.assertEqual(hello["pid"], first.pid)
        self.assertEqual(Path(hello["prefix"]), Path(sys.prefix))
        gate = self.data / "delay-reply"
        gate.touch()
        task = asyncio.create_task(self.request("broker"))
        tree = self.data / "broker-tree"
        async with asyncio.timeout(5):
            while not tree.is_file():
                await asyncio.sleep(0.01)
        pids = json.loads(tree.read_text())
        self.assertEqual(pids[0], first.pid)
        self.assertNotEqual(pids[0], pids[-1])
        with ExitStack() as handles:
            watched = []
            for pid in set(pids):
                handle = _winapi.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
                handles.callback(_winapi.CloseHandle, handle)
                self.assertEqual(
                    _winapi.WaitForSingleObject(handle, 0), _winapi.WAIT_TIMEOUT
                )
                watched.append(handle)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            for handle in watched:
                self.assertEqual(
                    await asyncio.to_thread(_winapi.WaitForSingleObject, handle, 5000),
                    _winapi.WAIT_OBJECT_0,
                )
        gate.unlink()
        new = await self.request("next")
        self.assertNotEqual(new["pid"], first.pid)
        self.assertEqual(new["pid"], self.module.process.pid)

    async def test_private_account_handler_needs_observed_conversation_kind(self):
        for profile in ("genshin", "starrail", "waves"):
            module = self.adapter.Module(profile, None)
            handlers = module.handlers().capabilities
            self.assertTrue(handlers["account"].private)
            self.assertEqual(handlers["account"].capability_id, "account")
            self.assertEqual(handlers["query"].private, profile == "waves")
            for kind, expected in [
                (None, ""),
                ("unknown", ""),
                ("direct", "direct"),
                ("group", "group"),
            ]:
                event = module.event(
                    SimpleNamespace(conversation_kind=kind, conversation_id="room"),
                    True,
                )
                self.assertEqual(event["conversation_kind"], expected)
                self.assertEqual(bool(event["group_id"]), kind == "group")

    async def test_stop_during_recovery_hello_reaps_replacement(self):
        (self.data / "delay-start").touch()
        with self.assertRaises(EOFError):
            await self.request("exit")
        await self.wait_for("boot", 2)
        pending = asyncio.create_task(self.request("pending"))
        await asyncio.sleep(0)
        await asyncio.wait_for(self.module.stop(), 5)
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertIsNone(self.module.process)
        self.assertEqual(len(self.records("boot")), 2)
        self.assertEqual(self.records("pending"), [])
        with self.assertRaises(self.adapter.ygl.ServiceUnavailable):
            await self.request("stale")

    async def test_account_rejects_forged_or_unbound_invocation(self):
        sdk = self.adapter.ygl
        handler = self.module.handlers().capabilities["account"]
        ctx = SimpleNamespace(
            origin="command",
            parent_id=None,
            capability_id="account",
            actor_id="actor",
            adapter_id="adapter",
            conversation_id="dm",
            conversation_kind="direct",
        )

        async def bind(view):
            return SimpleNamespace(invocation=ctx)

        self.module.services = SimpleNamespace(scopes=SimpleNamespace(bind=bind))
        for kind in (None, "group"):
            ctx.conversation_kind = kind
            with self.assertRaises(sdk.AccessDenied):
                await handler.invoke(ctx, {"text": "denied"})
        ctx.conversation_kind = "direct"
        req = {
            "text": "probe",
            "private_image": True,
            "capability_id": "account",
            "event": self.module.event(ctx, True),
        }
        with self.assertRaises(sdk.AccessDenied):
            await self.module.exchange({**req, "event": {}}, owner_context=ctx)
        with self.assertRaises(sdk.AccessDenied):
            await self.module.exchange(
                {**req, "capability_id": "direct"}, owner_context=ctx
            )
        self.assertFalse(
            (await self.module.exchange({**req, "owner_action": True}))["owner"]
        )
        self.assertTrue((await self.module.exchange(req, owner_context=ctx))["owner"])
        self.assertEqual(self.records("denied"), [])

    async def test_owner_image_descriptor_omits_raw_account_arguments(self):
        sdk = self.adapter.ygl
        sdk.CacheVisibility = SimpleNamespace(USER="user")
        sdk.cards = SimpleNamespace(
            Asset=lambda *args: args, Image=lambda *args, **kw: args
        )
        sdk.ImageBlock = lambda *args: args
        sdk.DisplayDocument = lambda *args, **kw: (args, kw)
        ctx = object()
        stored = []

        async def store(mime, raw, *, visibility, ttl_seconds):
            stored.append((mime, visibility))
            return SimpleNamespace(asset_id="private-image")

        async def bind(view):
            return SimpleNamespace(
                invocation=view, resources=SimpleNamespace(store=store)
            )

        self.module.services = SimpleNamespace(scopes=SimpleNamespace(bind=bind))
        # The output adapter validates the PNG signature. Use a complete 1x1 PNG.
        data = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9WlWW9sAAAAASUVORK5CYII="
        self.assertTrue(base64.b64decode(data).startswith(b"\x89PNG"))
        doc = (
            await self.module.handlers()
            .capabilities["account"]
            .document(
                ctx, "private-input-marker", [{"image": data}], sdk.Privacy.PRIVATE
            )
        )
        self.assertNotIn("private-input-marker", repr(doc))
        self.assertIn("账户操作结果", repr(doc))
        self.assertEqual(stored, [("image/png", "user")])

    async def test_queue_and_owner_proof_cancellation_leave_process_intact(self):
        original = self.module.process
        gate = self.data / "delay-reply"
        gate.touch()
        active = asyncio.create_task(self.request("active"))
        await self.wait_for("active")
        queued = asyncio.create_task(self.request("queued"))
        await asyncio.sleep(0)
        queued.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await queued
        gate.unlink()
        self.assertEqual((await active)["pid"], original.pid)
        binding = asyncio.Event()

        async def bind(_):
            binding.set()
            await asyncio.Future()

        self.module.services = SimpleNamespace(scopes=SimpleNamespace(bind=bind))
        waiting = asyncio.create_task(
            self.module.exchange({"text": "owner"}, owner_context=object())
        )
        await binding.wait()
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.assertEqual((await self.request("next"))["pid"], original.pid)
        self.assertEqual(self.records("queued") + self.records("owner"), [])
        self.assertEqual(self.records("boot"), [original.pid])

    async def test_indeterminate_delivery_recovers_without_replay(self):
        for label, exc in (("exit", EOFError), ("bad", ValueError)):
            original = self.module.process
            with self.assertRaises(exc):
                await asyncio.wait_for(self.request(label), 5)
            next_reply = await asyncio.wait_for(self.request("next-" + label), 5)
            self.assertNotEqual(next_reply["pid"], original.pid)
            self.assertEqual(self.records(label), [original.pid])
            self.assertIsNotNone(original.returncode)

    async def test_cancel_after_write_and_cancel_while_restoring(self):
        first = self.module.process
        reply = self.data / "delay-reply"
        reply.touch()
        startup = self.data / "delay-start"
        startup.touch()
        active = asyncio.create_task(self.request("written"))
        await self.wait_for("written")
        active.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await active
        await self.wait_for("boot", 2)
        unsent = asyncio.create_task(self.request("unsent"))
        await asyncio.sleep(0)
        unsent.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await unsent
        reply.unlink()
        startup.unlink()
        result = await asyncio.wait_for(self.request("survivor"), 5)
        self.assertEqual(self.records("boot"), [first.pid, result["pid"]])
        self.assertEqual(self.records("written"), [first.pid])
        self.assertEqual(self.records("unsent"), [])

    async def test_retired_generation_and_queued_exchange_cannot_restart(self):
        retired = self.adapter.Command(self.module, "query", True)
        gate = self.data / "delay-reply"
        gate.touch()
        active = asyncio.create_task(self.request("written"))
        await self.wait_for("written")
        queued = asyncio.create_task(self.request("queued"))
        await asyncio.sleep(0)
        await self.module.stop()
        with self.assertRaises(asyncio.CancelledError):
            await active
        with self.assertRaises(self.adapter.ygl.ServiceUnavailable):
            await queued
        with self.assertRaises(self.adapter.ygl.ServiceUnavailable):
            await self.request("after")
        self.assertEqual(len(self.records("boot")), 1)
        gate.unlink()
        await self.module.start()
        with self.assertRaises(self.adapter.ygl.ServiceUnavailable):
            await retired.invoke(None, {"text": "stale"})
        self.assertEqual(
            self.records("queued") + self.records("after") + self.records("stale"), []
        )
