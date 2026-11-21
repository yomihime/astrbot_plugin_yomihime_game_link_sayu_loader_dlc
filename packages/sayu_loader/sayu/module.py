"""Bind one immutable worker and upstream source tree to a YGL module generation."""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from .. import _api_v2 as ygl
from .runtime import WORKERS


MAX_REPLY = 6 * 1024 * 1024


class Factory:
    async def create(self, services):
        return Module(self.key, services)


class StarRailFactory(Factory):
    key = "starrail"


class GenshinFactory(Factory):
    key = "genshin"


class WavesFactory(Factory):
    key = "waves"


def stage(binding, target):
    exe, worker, worker_hash, source, source_hash = binding
    if not Path(exe).is_file():
        raise FileNotFoundError("private interpreter unavailable")
    local = target / "worker.pyz"
    shutil.copyfile(worker, local)
    with local.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != worker_hash:
            raise ValueError("worker digest changed")
    with Path(source).open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != source_hash:
            raise ValueError("upstream source archive digest changed")
        stream.seek(0)
        root = target / "source"
        with zipfile.ZipFile(stream) as bundle:
            for member in bundle.infolist():
                path = PurePosixPath(member.filename)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or any(char in member.filename for char in ("\\", ":"))
                ):
                    raise ValueError("invalid source path")
                bundle.extract(member, root)


class Module:
    def __init__(self, key, services):
        self.key = key
        self.services = services
        self.generation = None
        self.tasks = set()
        self.slots = asyncio.Semaphore(3)
        self.salt = os.urandom(32)
        self.lifecycle = asyncio.Lock()

    def handlers(self):
        # The original Waves query is owner-scoped. Public routes also expose
        # the SDK owner transport for copyable replies in a direct conversation.
        public = self.key != "waves"
        handlers = {"query": Command(self, not public)}
        if public:
            handlers["direct"] = Command(self, True)
        return ygl.ModuleHandlers(handlers, {}, {})

    async def start(self):
        async with self.lifecycle:
            if self.generation is not None:
                return
            folder = tempfile.TemporaryDirectory(prefix="sayu-generation-")
            staging = asyncio.create_task(
                asyncio.to_thread(stage, WORKERS[self.key], Path(folder.name))
            )
            try:
                await asyncio.shield(staging)
            except BaseException:
                await asyncio.gather(staging, return_exceptions=True)
                folder.cleanup()
                raise
            self.generation = folder

    async def stop(self):
        async with self.lifecycle:
            generation, self.generation = self.generation, None
            pending = tuple(self.tasks)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if generation is not None:
                await asyncio.to_thread(generation.cleanup)

    async def check_health(self):
        ready = self.generation is not None and Path(WORKERS[self.key][0]).is_file()
        status = ygl.HealthStatus.AVAILABLE if ready else ygl.HealthStatus.UNAVAILABLE
        return ygl.HealthReport(
            {key: ygl.CapabilityHealth(status) for key in self.handlers().capabilities}
        )

    def event(self, context, private=False):
        def opaque(field):
            value = getattr(context, field, None)
            if value is None:
                return ""
            return hmac.new(self.salt, str(value).encode(), "sha256").hexdigest()[:24]

        kind = getattr(context, "conversation_kind", None)
        if kind not in ("group", "direct"):
            kind = "direct" if private else ""
        return {
            "actor_id": opaque("actor_id"),
            "conversation_kind": kind,
            "adapter_id": getattr(context, "adapter_id", None) or "astrbot",
            "group_id": opaque("conversation_id") if kind == "group" else "",
            "message_id": opaque("invocation_id"),
        }


async def exchange(exe, generation, request, work):
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() in {"SYSTEMROOT", "WINDIR", "LANG"}
    }
    env.update(TMP=work, TEMP=work, TMPDIR=work)
    process = await asyncio.create_subprocess_exec(
        exe,
        "-I",
        "-B",
        generation / "worker.pyz",
        generation / "source",
        cwd=work,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        process.stdin.write(json.dumps(request).encode())
        await process.stdin.drain()
        process.stdin.close()
        data = bytearray()
        while chunk := await process.stdout.read(65536):
            if len(data) + len(chunk) > MAX_REPLY:
                raise ValueError("worker response too large")
            data.extend(chunk)
        if await process.wait():
            raise ValueError("worker exited")
        return json.loads(data)
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            # Drain piped output after a bounded read or cancellation. A full
            # asyncio pipe can otherwise keep wait() pending during retirement.
            while await process.stdout.read(65536):
                pass
            await process.wait()


ERRORS = {
    "no_match": (ygl.ErrorCode.NOT_FOUND, "未匹配到当前已加载的上游指令。"),
    "missing_port": (ygl.ErrorCode.UNSUPPORTED, "此上游功能需要尚未接入的接口或依赖。"),
    "unsupported_message": (
        ygl.ErrorCode.UNSUPPORTED,
        "此上游回复超出了当前投递能力。",
    ),
    "upstream_error": (ygl.ErrorCode.UPSTREAM_ERROR, "上游处理指令时发生错误。"),
    "timeout": (ygl.ErrorCode.UPSTREAM_ERROR, "上游处理指令超时。"),
}


class Command:
    def __init__(self, module, private):
        self.module = module
        self.private = private

    async def document(self, context, text, parts, privacy):
        if not isinstance(parts, list) or not 1 <= len(parts) <= 64:
            raise ValueError("invalid message chain")
        texts, blocks, images = [], [], []
        for part in parts:
            if isinstance(part, dict) and part.keys() == {"text"}:
                value = part["text"]
                if not isinstance(value, str) or not value.strip():
                    raise ValueError("invalid text")
                texts.append(value)
                blocks.append(ygl.cards.Text(value))
            elif isinstance(part, dict) and part.keys() == {"image"}:
                raw = base64.b64decode(part["image"], validate=True)
                if raw.startswith(b"\xff\xd8\xff"):
                    kind = "jpeg"
                elif raw.startswith(b"\x89PNG\r\n\x1a\n"):
                    kind = "png"
                else:
                    raise ValueError("unsupported image format")
                images.append(raw)
                blocks.append(
                    ygl.cards.Image(ygl.cards.Asset(kind, raw, text), height=640)
                )
            else:
                raise ValueError("invalid worker message part")
        card = None
        fallback = (ygl.TextBlock("\n\n".join(texts) or text),)
        if images and privacy is ygl.Privacy.PRIVATE:
            if len(parts) != 1 or not images[0].startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("owner transport requires a single PNG")
            scope = await self.module.services.scopes.bind(context)
            ref = await scope.resources.store(
                "image/png",
                images[0],
                visibility=ygl.CacheVisibility.USER,
                ttl_seconds=300,
            )
            fallback = (ygl.ImageBlock(ref.asset_id, text, privacy, text, True),)
        elif images:
            card = ygl.cards.Document(text, tuple(blocks))
        return ygl.DisplayDocument(
            "Sayu Loader", self.module.key, fallback, card=card, privacy=privacy
        )

    async def invoke(self, context, parameters):
        module = self.module
        if module.generation is None:
            raise ygl.ServiceUnavailable()
        privacy = ygl.Privacy.PRIVATE if self.private else ygl.Privacy.PUBLIC
        task = asyncio.current_task()
        module.tasks.add(task)
        error = "upstream_error"
        try:
            text = parameters["text"]
            request = {
                "text": text,
                "event": module.event(context, self.private),
                "private_image": privacy is ygl.Privacy.PRIVATE,
            }
            async with asyncio.timeout(20), module.slots:
                generation = Path(module.generation.name)
                with tempfile.TemporaryDirectory(prefix="sayu-chat-") as work:
                    reply = await exchange(
                        WORKERS[module.key][0], generation, request, work
                    )
                if "error" not in reply:
                    document = await self.document(
                        context, text, reply["parts"], privacy
                    )
                    return ygl.CapabilityResult(
                        "sayu-command",
                        ygl.ResultStatus.SUCCESS,
                        document,
                        privacy=privacy,
                    )
                error = reply["error"]
        except TimeoutError:
            error = "timeout"
        except (OSError, ValueError, TypeError, KeyError):
            pass
        finally:
            module.tasks.discard(task)
        code, message = ERRORS.get(error, ERRORS["upstream_error"])
        return ygl.CapabilityResult(
            "sayu-error",
            ygl.ResultStatus.ERROR,
            error=ygl.ErrorDetail(code, message),
            privacy=privacy,
        )
