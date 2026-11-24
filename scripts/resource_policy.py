"""Collect static resource declarations from the checksum-verified game archive."""

import ast
from hashlib import file_digest
import re
from zipfile import ZipFile

IMAGE = re.compile(
    r"https://[A-Za-z0-9.-]+/[A-Za-z0-9_./%~-]+\.(?:png|jpe?g|webp|gif|avif)", re.I
)
DIRECTORY = re.compile(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)+")


def _values(fn, expression):
    if not isinstance(expression, ast.Name):
        return [expression]
    return [
        n.value
        for n in ast.walk(fn)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == expression.id for t in n.targets)
    ]


def from_snapshot(path, lock):
    images, directories = set(), set()
    with path.open("rb") as stream:
        if file_digest(stream, "sha256").hexdigest() != lock["snapshot_sha256"]:
            raise ValueError("resource declarations require the pinned source archive")
        stream.seek(0)
        with ZipFile(stream) as source:
            for name in source.namelist():
                if not name.startswith(lock["package"] + "/") or not name.endswith(
                    ".py"
                ):
                    continue
                body = source.read(name)
                if b"https://" not in body and b"download_all_file" not in body:
                    continue
                tree = ast.parse(body)
                fragments = {
                    id(value)
                    for n in ast.walk(tree)
                    if isinstance(n, ast.JoinedStr)
                    for value in n.values
                }
                images.update(
                    node.value
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Constant)
                    and id(node) not in fragments
                    and isinstance(node.value, str)
                    and IMAGE.fullmatch(node.value)
                )
                imports = {
                    a.asname or a.name
                    for n in tree.body
                    if isinstance(n, ast.ImportFrom)
                    and n.level == 0
                    and n.module == "gsuid_core.utils.download_resource.download_core"
                    for a in n.names
                    if a.name == "download_all_file"
                }
                if not imports:
                    continue
                for fn in ast.walk(tree):
                    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    for call in ast.walk(fn):
                        if not (
                            isinstance(call, ast.Call)
                            and isinstance(call.func, ast.Name)
                            and call.func.id in imports
                            and len(call.args) >= 2
                        ):
                            continue
                        if not any(
                            isinstance(owner, ast.Constant)
                            and owner.value == lock["package"]
                            for owner in _values(fn, call.args[0])
                        ):
                            continue
                        for value in _values(fn, call.args[1]):
                            if isinstance(value, ast.Dict):
                                directories.update(
                                    key.value
                                    for key in value.keys
                                    if isinstance(key, ast.Constant)
                                    and isinstance(key.value, str)
                                    and DIRECTORY.fullmatch(key.value)
                                )
    return sorted(images), sorted(directories)
