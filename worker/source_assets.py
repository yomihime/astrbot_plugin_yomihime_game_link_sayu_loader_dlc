"""Bounded reads, private JSON mirrors and indexed assets for a single chat worker."""

import json
from contextlib import asynccontextmanager, contextmanager
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace

from declarations import Unsupported


class Assets:
    def __init__(self, source):
        self.root = source.resolve()
        self.data_root = None
        self.public = None
        self.index = None
        self.index_root = None
        self.fetched = set()
        self.private_json = set()

    def begin_invocation(self):
        if self.public is not None:
            self.public.attempted.clear()
        if self.index is not None:
            self.index.begin_invocation()

    def contains(self, candidate):
        return candidate.is_relative_to(self.root) or (
            self.data_root is not None and candidate.is_relative_to(self.data_root)
        )

    def path(self, value):
        candidate = Path(value).resolve()
        if not self.contains(candidate):
            raise ValueError("path outside plugin assets")
        return AssetPath(self, candidate)

    def materialize(self, value):
        path = self.path(value)._path
        if (
            not path.exists()
            and self.public is not None
            and self.data_root is not None
            and path.is_relative_to(self.data_root)
        ):
            if self.public.ensure(self.data_root, path) or (
                self.index
                and path.is_relative_to(self.index_root)
                and self.index.ensure(path)
            ):
                if path.is_file():
                    self.fetched.add(path)
        return path

    def file(self, value):
        if not isinstance(value, AssetPath) or value.assets is not self:
            raise ValueError("expected a plugin asset")
        path = self.materialize(value)
        if path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("asset exceeds read limit")
        return path

    @contextmanager
    def write_json(self, path, encoding):
        from tempfile import NamedTemporaryFile

        target = self.path(path)._path
        if (
            target not in self.private_json
            or self.data_root is None
            or not target.is_relative_to(self.data_root)
        ):
            raise Unsupported("not a declared private JSON destination")
        with StringIO() as stream:
            yield stream
            contents = stream.getvalue().encode(encoding or "utf-8")
        if len(contents) > 4 * 1024 * 1024:
            raise ValueError("private JSON exceeds write limit")
        json.loads(contents)
        target.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=target.parent, delete=False) as temp:
            pending = Path(temp.name)
            try:
                temp.write(contents)
            except BaseException:
                temp.close()
                pending.unlink(missing_ok=True)
                raise
        try:
            pending.replace(target)
        finally:
            pending.unlink(missing_ok=True)

    def open(self, path, mode="r", *, encoding=None):
        if mode == "w":
            return self.write_json(path, encoding)
        if mode not in {"rb", "r"}:
            raise ValueError("expected read mode")
        return self.file(path).open(mode, encoding=encoding)

    @asynccontextmanager
    async def open_async(self, path, mode="r", *, encoding=None):
        with self.open(path, mode, encoding=encoding) as stream:

            async def read():
                return stream.read()

            yield SimpleNamespace(read=read)

    def image(self, path):
        from PIL import Image

        from sayu_compat import MAX_INPUT_PIXELS

        picture = Image.open(BytesIO(self.file(path).read_bytes()))
        if picture.width * picture.height > MAX_INPUT_PIXELS:
            picture.close()
            raise ValueError("asset exceeds pixel limit")
        return picture

    def font(self, path, size, *, index=0):
        from PIL import ImageFont

        if not 1 <= size <= 512:
            raise ValueError("font size outside supported range")
        file = self.file(self.path(path))
        if file.suffix.lower() not in {".ttf", ".ttc", ".otf"}:
            raise ValueError("expected a packaged font")
        return ImageFont.truetype(BytesIO(file.read_bytes()), size=size, index=index)


class AssetPath:
    """Keep all lookups, including input-derived filenames, inside this source tree."""

    def __init__(self, assets, value):
        self.assets = assets
        self._path = value

    def __fspath__(self):
        return str(self._path)

    def __str__(self):
        return str(self._path)

    def __truediv__(self, part):
        return self.assets.path(self._path / part)

    @property
    def parent(self):
        return self.assets.path(self._path.parent)

    @property
    def parents(self):
        result = []
        for parent in self._path.parents:
            if not self.assets.contains(parent):
                break
            result.append(self.assets.path(parent))
        return tuple(result)

    @property
    def name(self):
        return self._path.name

    @property
    def stem(self):
        return self._path.stem

    def _entries(self, pattern, recursive):
        if Path(pattern).name != pattern or "**" in pattern:
            raise ValueError("expected asset filename pattern")
        paths = set(
            self._path.rglob(pattern) if recursive else self._path.glob(pattern)
        )
        if self.assets.index and self._path.is_relative_to(self.assets.index_root):
            paths.update(
                self.assets.index.entries(self._path, pattern, recursive=recursive)
            )
        return [self.assets.path(path) for path in sorted(paths)]

    def glob(self, pattern):
        return self._entries(pattern, recursive=False)

    def rglob(self, pattern):
        return self._entries(pattern, recursive=True)

    def open(self, mode="r", *, encoding=None):
        return self.assets.open(self, mode, encoding=encoding)

    def unlink(self, *, missing_ok=False):
        if self._path not in self.assets.fetched:
            raise Unsupported("only this request's downloaded asset can be discarded")
        self.assets.path(self)._path.unlink(missing_ok=missing_ok)
        self.assets.fetched.discard(self._path)

    def exists(self):
        return self.assets.materialize(self).exists()

    def is_file(self):
        return self.assets.materialize(self).is_file()
