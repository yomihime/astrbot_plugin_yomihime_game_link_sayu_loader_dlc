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
        self.binding = None
        self.recovery = None
        self.retiring = None
        self.active_exchange = None

    def handlers(self):
        # The original Waves query is owner-scoped. Public routes also expose
        # the SDK owner transport for copyable replies in a direct conversation.
        public = self.key != "waves"
        handlers = {
            "query": Command(self, "query", not public),
            "account": Command(self, "account", True),
        }
        if public:
            handlers["direct"] = Command(self, "direct", True)
        return ygl.ModuleHandlers(handlers, {}, {})

    async def _launch(self, binding):
        folder = tempfile.TemporaryDirectory(prefix="sayu-generation-")
        root = Path(folder.name)
        staging = asyncio.create_task(asyncio.to_thread(stage, binding, root))
        process = None
        try:
            await asyncio.shield(staging)
            exe, *_, data = binding
            spawning = asyncio.create_task(spawn(exe, root, data))
            try:
                process = await asyncio.shield(spawning)
            except asyncio.CancelledError:
                process = await spawning
                raise
            async with asyncio.timeout(60):
                hello = await read_frame(process)
            if hello.get("ready") is not True:
                raise RuntimeError(
                    f"worker startup failed: {hello.get('error')} ({hello.get('type')})"
                )
            salt = await asyncio.to_thread(profile_salt, data)
            return folder, process, salt
        except BaseException:
            await asyncio.gather(staging, return_exceptions=True)
            if process is not None:
                await close_worker(process, abort=True)
            folder.cleanup()
            raise

    async def start(self):
        async with self.lifecycle:
            if self.generation is None:
                binding = tuple(WORKERS[self.key])
                self.generation, self.process, self.salt = await self._launch(binding)
                self.binding = binding

    async def _restore(self, revision):
        if self.retiring is not None:
            await asyncio.shield(self.retiring)
        if revision != self.revision or self.generation is None:
            raise ygl.ServiceUnavailable()
        previous = self.generation
        self.generation, self.process, self.salt = await self._launch(self.binding)
        previous.cleanup()

    def _begin_recovery(self):
        if self.recovery is None or self.recovery.done():
            self.recovery = asyncio.create_task(self._restore(self.revision))
            # Recovery may finish between commands with no waiter.
            self.recovery.add_done_callback(
                lambda task: None if task.cancelled() else task.exception()
            )
        return self.recovery

    async def stop(self):
        async with self.lifecycle:
            generation, self.generation = self.generation, None
            self.revision += 1
            pending = set(self.tasks)
            if self.active_exchange is not None:
                pending.add(self.active_exchange)
            if self.recovery is not None:
                pending.add(self.recovery)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if self.retiring is not None:
                await self.retiring
            self.recovery = self.retiring = self.binding = None
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

    async def exchange(self, request, *, owner_context=None):
        revision = self.revision
        async with self.io:
            if self.generation is None or revision != self.revision:
                raise ygl.ServiceUnavailable()
            self.active_exchange = asyncio.current_task()
            try:
                if self.process is None or self.process.returncode is not None:
                    try:
                        await asyncio.shield(self._begin_recovery())
                    except (RuntimeError, OSError, EOFError, ValueError) as exc:
                        raise ygl.ServiceUnavailable() from exc
                # This bit is minted only after binding the live SDK invocation.
                request = {k: v for k, v in request.items() if k != "owner_action"}
                if owner_context is not None:
                    # Recheck Core's live owner proof after waiting for the worker.
                    scope = await self.services.scopes.bind(owner_context)
                    if (
                        scope.invocation is not owner_context
                        or getattr(owner_context, "origin", None) != "command"
                        or getattr(owner_context, "parent_id", None) is not None
                        or request.get("private_image") is not True
                        or request.get("capability_id")
                        != getattr(owner_context, "capability_id", None)
                        or request.get("event") != self.event(owner_context, True)
                    ):
                        raise ygl.AccessDenied()
                    request = {**request, "owner_action": True}
                encoded = (
                    json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n"
                )
                if len(encoded) > 16384:
                    raise ValueError("worker request too large")
                process = self.process
                if revision != self.revision or process is None:
                    raise ygl.ServiceUnavailable()
                try:
                    process.stdin.write(encoded)
                    await process.stdin.drain()
                    return await read_frame(process)
                except BaseException:
                    # Delivery is indeterminate from the first write. This request
                    # ends here. A fresh interpreter serves subsequent commands.
                    self.process = None
                    self.retiring = asyncio.create_task(
                        close_worker(process, abort=True)
                    )
                    if self.generation is not None and revision == self.revision:
                        self._begin_recovery()
                    await asyncio.shield(self.retiring)
                    raise
            finally:
                self.active_exchange = None

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
            kind = ""
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
    if os.name == "nt":
        executable = Path(exe).absolute()
        config = executable.parent.parent / "pyvenv.cfg"
        if executable.parent.name.casefold() == "scripts" and config.is_file():
            # Windows venv's python.exe normally redirects to a second process.
            # Launch the configured base interpreter with the same CPython venv
            # marker so the pipe, PID, and forced retirement have one owner.
            settings = dict(
                (key.strip().casefold(), value.strip())
                for line in config.read_text(encoding="utf-8").splitlines()
                for key, sep, value in [line.partition("=")]
                if sep
            )
            base = Path(settings["home"]) / executable.name
            if not base.is_absolute() or not base.is_file():
                raise FileNotFoundError("private base interpreter unavailable")
            env["__PYVENV_LAUNCHER__"] = str(executable)
            exe = str(base)
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
    def __init__(self, module, capability_id, private):
        self.module = module
        self.capability_id = capability_id
        self.private = private
        self.revision = module.revision

    async def document(self, context, text, parts, privacy):
        if self.capability_id == "account":
            # Raw account arguments can contain credentials. Asset descriptions
            # and fallbacks only need a stable label for the private response.
            text = "账户操作结果"
        if not isinstance(parts, list) or not 1 <= len(parts) <= 64:
            raise ValueError("invalid message chain")
        if any(isinstance(part, dict) and "poster" in part for part in parts):
            audience = ygl.Privacy.PRIVATE if self.private else ygl.Privacy.PUBLIC
            if (
                privacy is not audience
                or len(parts) != 1
                or parts[0].keys() != {"poster"}
            ):
                raise UnsupportedProjection(
                    "image pages require one supported audience and a single poster"
                )
            poster = parts[0]["poster"]
            if not isinstance(poster, dict) or poster.keys() != {
                "width",
                "height",
                "tiles",
            }:
                raise UnsupportedProjection("invalid image pages")
            width, height, tiles = poster["width"], poster["height"], poster["tiles"]
            if (
                type(width) is not int
                or type(height) is not int
                or not (
                    1 <= width <= 1536
                    and 1 <= height <= 8192
                    and width * height <= 8_000_000
                )
            ):
                raise UnsupportedProjection("invalid image dimensions")
            grid_width = max(400, width)
            columns = max(8, (grid_width + 95) // 96)
            cell_width = (grid_width + columns - 1) // columns
            cell_height = 128
            expected = columns * ((height + cell_height - 1) // cell_height)
            if not isinstance(tiles, list) or len(tiles) != expected or expected > 1000:
                raise UnsupportedProjection("incomplete image")
            items = []
            total = 0
            for i, encoded in enumerate(tiles):
                raw = base64.b64decode(encoded, validate=True)
                total += len(raw)
                if (
                    total > 4 * 1024 * 1024
                    or len(raw) > 1024 * 1024
                    or raw[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
                    or int.from_bytes(raw[16:20], "big") != cell_width
                    or int.from_bytes(raw[20:24], "big") != cell_height
                ):
                    raise UnsupportedProjection("invalid image tile")
                items.append(
                    ygl.cards.DenseCollageItem(
                        str(i + 1), 1, ygl.cards.Asset("png", raw)
                    )
                )
            pages = ygl.cards.DenseCollage(
                tuple(items),
                columns=columns,
                cell_width=cell_width,
                cell_height=cell_height,
                max_height=1024,
                max_pages=8,
                fit="contain",
            )
            card = ygl.cards.Document(
                text,
                (pages,),
                width=columns * cell_width,
                subtitle="同一张原图按从上到下的顺序阅读",
            )
            return ygl.DisplayDocument(
                "Sayu Loader",
                self.module.key,
                text_blocks(["原图未完整显示。"], self.module.key),
                card=card,
                privacy=privacy,
            )
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
                "capability_id": self.capability_id,
                "event": module.event(context, self.private),
                "private_image": privacy is ygl.Privacy.PRIVATE,
            }
            # Core owns the dispatch deadline, including time spent in the worker queue.
            owner_command = (
                self.private
                and getattr(context, "origin", None) == "command"
                and context.parent_id is None
                and context.capability_id == self.capability_id
                and context.actor_id
                and context.adapter_id
                and context.conversation_id
            )
            if self.capability_id == "account" and (
                not owner_command
                or request["event"]["conversation_kind"] != "direct"
            ):
                raise ygl.AccessDenied()
            if owner_command:
                reply = await module.exchange(request, owner_context=context)
            else:
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
        except (ygl.AccessDenied, ygl.InvalidInvocation):
            raise
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
