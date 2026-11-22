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
    exe, worker, worker_hash, source, source_hash, _data = binding
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


def profile_salt(data):
    """The running worker holds the profile lock before this call."""
    path = Path(data).with_name(Path(data).name + ".identity")
    if not path.exists():
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            pending = Path(stream.name)
        try:
            pending.write_bytes(os.urandom(32))
            os.replace(pending, path)
        finally:
            pending.unlink(missing_ok=True)
    salt = path.read_bytes()
    if len(salt) != 32:
        raise ValueError("invalid profile identity salt")
    return salt


class Module:
    def __init__(self, key, services):
        self.key = key
        self.services = services
        self.generation = None
        self.tasks = set()
        self.revision = 0
        self.io = asyncio.Lock()
        self.process = None
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
                root = Path(folder.name)
                exe, *_, data = WORKERS[self.key]
                spawning = asyncio.create_task(spawn(exe, root, data))
                try:
                    self.process = await asyncio.shield(spawning)
                except asyncio.CancelledError:
                    self.process = await spawning
                    raise
                async with asyncio.timeout(60):
                    hello = await read_frame(self.process)
                if hello.get("ready") is not True:
                    raise RuntimeError(
                        f"worker startup failed: {hello.get('error')} ({hello.get('type')})"
                    )
                self.salt = await asyncio.to_thread(profile_salt, data)
            except BaseException:
                await asyncio.gather(staging, return_exceptions=True)
                if self.process is not None:
                    await close_worker(self.process, abort=True)
                    self.process = None
                folder.cleanup()
                raise
            self.generation = folder

    async def stop(self):
        async with self.lifecycle:
            generation, self.generation = self.generation, None
            self.revision += 1
            pending = tuple(self.tasks)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            process, self.process = self.process, None
            try:
                if process is not None:
                    await close_worker(process)
            finally:
                if generation is not None:
                    await asyncio.to_thread(generation.cleanup)

    async def check_health(self):
        ready = (
            self.generation is not None
            and self.process is not None
            and self.process.returncode is None
        )
        status = ygl.HealthStatus.AVAILABLE if ready else ygl.HealthStatus.UNAVAILABLE
        return ygl.HealthReport(
            {key: ygl.CapabilityHealth(status) for key in self.handlers().capabilities}
        )

    async def exchange(self, request):
        encoded = json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n"
        if len(encoded) > 16384:
            raise ValueError("worker request too large")
        async with self.io:
            process = self.process
            if (
                self.generation is None
                or process is None
                or process.returncode is not None
            ):
                raise ygl.ServiceUnavailable()
            try:
                process.stdin.write(encoded)
                await process.stdin.drain()
                return await read_frame(process)
            except BaseException:
                # An interrupted read can leave a response in flight. Retire the
                # transport before releasing the lock so no caller receives it.
                self.process = None
                await close_worker(process, abort=True)
                raise

    def event(self, context, private=False):
        def opaque(field):
            value = getattr(context, field, None)
            if value is None:
                return ""
            return hmac.new(
                self.salt,
                json.dumps(
                    [getattr(context, "adapter_id", None), field, str(value)]
                ).encode(),
                "sha256",
            ).hexdigest()[:24]

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


async def spawn(exe, generation, data):
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() in {"SYSTEMROOT", "WINDIR", "LANG"}
    }
    work = str(generation)
    env.update(TMP=work, TEMP=work, TMPDIR=work)
    return await asyncio.create_subprocess_exec(
        exe,
        "-I",
        "-B",
        generation / "worker.pyz",
        generation / "source",
        data,
        cwd=work,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )


async def read_frame(process):
    size = int.from_bytes(await process.stdout.readexactly(4), "big")
    if not 0 < size <= MAX_REPLY:
        raise ValueError("invalid worker response size")
    reply = json.loads(await process.stdout.readexactly(size))
    if not isinstance(reply, dict):
        raise ValueError("invalid worker response")
    return reply


async def close_worker(process, *, abort=False):
    process.stdin.close()
    if not abort:
        try:
            async with asyncio.timeout(5):
                await process.wait()
                if process.returncode:
                    raise RuntimeError("worker shutdown failed")
                return
        except TimeoutError:
            pass
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    while await process.stdout.read(65536):
        pass
    await process.wait()
    if not abort:
        raise TimeoutError("worker did not finish its shutdown hooks")


class UnsupportedProjection(ValueError):
    pass


def text_blocks(parts, subject):
    # GenericDisplayRenderer turns embedded control characters into spaces.
    # One DTO line must produce exactly one output line, including blank rows.
    joined = "\n\n".join(parts).replace("\r\n", "\n").replace("\r", "\n")
    lines = joined.split("\n")
    if (
        len(lines) + 2 > 200
        or len("Sayu Loader\n" + subject + "\n" + joined) > 6000
        or any((ord(c) < 32 and c != "\n") or c in "\u2028\u2029" for c in joined)
    ):
        raise UnsupportedProjection("text exceeds the host display limits")
    if len(lines) <= 32 and all(line.strip() for line in lines):
        return tuple(ygl.TextBlock(line) for line in lines)
    blocks = []
    start = 0
    while start < len(lines):
        if not lines[start].strip():
            raise UnsupportedProjection(
                "blank line cannot be represented as a block heading"
            )
        end = min(start + 51, len(lines))
        # The first line is a table heading. Keep the next nonempty heading
        # within reach without using an empty or fabricated heading.
        while end < len(lines) and end > start and not lines[end].strip():
            end -= 1
        if end == start or len(blocks) >= 32:
            raise UnsupportedProjection("text cannot fit the host table limits")
        blocks.append(
            ygl.TableBlock(
                (lines[start],), tuple((line,) for line in lines[start + 1 : end])
            )
        )
        start = end
    return tuple(blocks)


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
        self.revision = module.revision

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
                    raise UnsupportedProjection("unsupported image format")
                images.append(raw)
                blocks.append(
                    ygl.cards.Image(ygl.cards.Asset(kind, raw, text), height=640)
                )
            else:
                raise ValueError("invalid worker message part")
        card = None
        if images and privacy is ygl.Privacy.PRIVATE:
            if len(parts) != 1 or not images[0].startswith(b"\x89PNG\r\n\x1a\n"):
                raise UnsupportedProjection("owner transport requires a single PNG")
            scope = await self.module.services.scopes.bind(context)
            ref = await scope.resources.store(
                "image/png",
                images[0],
                visibility=ygl.CacheVisibility.USER,
                ttl_seconds=300,
            )
            fallback = (ygl.ImageBlock(ref.asset_id, text, privacy, text, True),)
        elif images:
            if len(images) != 1:
                raise UnsupportedProjection("public projection requires one image")
            fallback = text_blocks([*texts, "图片未显示。"], self.module.key)
            card = ygl.cards.Document(text, tuple(blocks))
        else:
            fallback = text_blocks(texts, self.module.key)
        return ygl.DisplayDocument(
            "Sayu Loader", self.module.key, fallback, card=card, privacy=privacy
        )

    async def invoke(self, context, parameters):
        module = self.module
        if module.generation is None or self.revision != module.revision:
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
            async with asyncio.timeout(20):
                reply = await module.exchange(request)
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
        except UnsupportedProjection:
            error = "unsupported_message"
        except TimeoutError:
            error = "timeout"
        except (OSError, EOFError, ValueError, TypeError, KeyError):
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
