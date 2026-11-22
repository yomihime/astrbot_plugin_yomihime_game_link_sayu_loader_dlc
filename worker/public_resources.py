"""Fetch individual PNGs from the fixed plugin's declared public resource directories."""

import ast
import logging
import os
import re
import tempfile
from http.client import HTTPException
from io import BytesIO
from pathlib import Path
from urllib.request import HTTPRedirectHandler, build_opener

from PIL import Image

from sayu_compat import MAX_INPUT_PIXELS

# HTTPS static mirror and URL layout used by the upstream download_all_file port:
# https://github.com/Genshin-bots/gsuid_core/blob/master/gsuid_core/utils/download_resource/download_core.py
MIRROR = "https://silverwing.elysia.beauty"
MAX_BYTES = 8 * 1024 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _declarations(source, plugin):
    if not plugin.isidentifier():
        raise ValueError("invalid plugin package")
    for path in (source / plugin).rglob("*.py"):
        raw = path.read_bytes()
        if b"gsuid_core.utils.download_resource.download_core" not in raw:
            continue
        tree = ast.parse(raw)
        loaders = {
            alias.asname or alias.name
            for node in tree.body
            if isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module == "gsuid_core.utils.download_resource.download_core"
            for alias in node.names
            if alias.name == "download_all_file"
        }
        parts = path.relative_to(source).with_suffix("").parts
        module = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
        for call in ast.walk(tree):
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id in loaders
                and len(call.args) == 2
                and not call.keywords
                and isinstance(call.args[0], ast.Constant)
                and call.args[0].value == plugin
                and isinstance(call.args[1], ast.Dict)
            ):
                continue
            for key, dest in zip(call.args[1].keys, call.args[1].values):
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and re.fullmatch(r"(?:wiki|guide)/[a-z_]+", key.value)
                    and isinstance(dest, ast.Name)
                ):
                    yield module, dest.id, key.value


def _read_png(url):
    with build_opener(_NoRedirect()).open(url, timeout=6) as response:
        if response.status != 200 or response.geturl() != url:
            raise ValueError("unexpected HTTP response")
        size = response.headers.get("Content-Length")
        if size and not 0 < int(size) <= MAX_BYTES:
            raise ValueError("invalid image size")
        raw = response.read(MAX_BYTES + 1)
        if not raw or len(raw) > MAX_BYTES or (size and len(raw) != int(size)):
            raise ValueError("invalid response length")
    with Image.open(BytesIO(raw), formats=("PNG",)) as image:
        if image.width * image.height > MAX_INPUT_PIXELS or getattr(
            image, "is_animated", False
        ):
            raise ValueError("unsupported image dimensions or frames")
        image.verify()
    return raw


class PublicResources:
    def __init__(self, source, plugin):
        self.plugin = plugin
        self.declarations = tuple(_declarations(source, plugin))
        self.directories = {}
        self.attempted = set()

    def bind(self, resolve, data_root):
        """Resolve the declared local destinations using the same upstream namespace."""
        if not data_root:
            return
        plugin_root = Path(data_root).resolve() / self.plugin
        for module, name, endpoint in self.declarations:
            try:
                path = Path(resolve(module, name)).resolve()
            except (ValueError, TypeError, KeyError, ImportError, OSError):
                continue
            if path.is_relative_to(plugin_root) and path != plugin_root:
                self.directories[path] = endpoint

    def ensure(self, data_root, path):
        """Materialize a missing upstream-derived ID only inside this request's data root."""
        if data_root is None or not self.directories or path.exists():
            return False
        root = Path(data_root).resolve()
        path = Path(path)
        if not path.is_relative_to(root) or path.resolve() != path:
            return False
        endpoint = self.directories.get(path.parent)
        if not endpoint or not re.fullmatch(r"[0-9]{1,12}\.png", path.name):
            return False
        # Repeated checks in this invocation reuse the first result.
        if path in self.attempted or len(self.attempted) >= 4:
            return False
        self.attempted.add(path)
        url = f"{MIRROR}/{self.plugin}/{endpoint}/{path.name}"
        try:
            raw = _read_png(url)
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
                pending = Path(tmp.name)
            try:
                pending.write_bytes(raw)
                os.replace(pending, path)
            finally:
                pending.unlink(missing_ok=True)
        except (
            OSError,
            HTTPException,
            ValueError,
            SyntaxError,
            Image.DecompressionBombError,
        ) as exc:
            logging.getLogger("sayu").warning(
                "Public PNG unavailable (%s)", type(exc).__name__
            )
            return False
        return True
