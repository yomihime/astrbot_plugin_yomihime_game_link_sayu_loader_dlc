"""Collect static resource declarations from the checksum-verified game archive."""

import ast
from hashlib import file_digest, sha256
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


def _constant_string(node, bindings):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return bindings.get(node.id)
    if not isinstance(node, ast.JoinedStr):
        return None
    parts = []
    for item in node.values:
        if isinstance(item, ast.FormattedValue):
            if (
                item.conversion != -1
                or item.format_spec
                or not isinstance(item.value, ast.Name)
            ):
                return None
            part = bindings.get(item.value.id)
        elif isinstance(item, ast.Constant) and isinstance(item.value, str):
            part = item.value
        else:
            return None
        if part is None:
            return None
        parts.append(part)
    return "".join(parts)


def _static_table_images(tree):
    """Resolve adjacent module-level literal tables without executing plugin code."""
    bindings = {}
    for statement in tree.body:
        if not (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
        ):
            bindings.clear()
            continue
        value = statement.value
        # Calls, attribute lookups, comprehensions and assignments inside an
        # expression can change names. Only descend into plain literal tables.
        literal_types = (
            ast.Constant,
            ast.Name,
            ast.Load,
            ast.List,
            ast.Tuple,
            ast.Dict,
            ast.JoinedStr,
            ast.FormattedValue,
        )
        nodes = list(ast.walk(value))
        if any(
            not isinstance(n, literal_types)
            or (isinstance(n, ast.Name) and n.id not in bindings)
            or (
                isinstance(n, ast.FormattedValue)
                and (n.conversion != -1 or n.format_spec)
            )
            for n in nodes
        ):
            bindings.clear()
            continue
        for node in nodes:
            if isinstance(node, ast.JoinedStr):
                resolved = _constant_string(node, bindings)
                if resolved and len(resolved) <= 2048 and IMAGE.fullmatch(resolved):
                    yield resolved
        text = _constant_string(value, bindings)
        target = statement.targets[0].id
        if text is not None and len(text) <= 2048:
            bindings[target] = text
        else:
            bindings.pop(target, None)


def _stream_objects(body, declaration, mirrors, package):
    if sha256(body).hexdigest() != declaration["sha256"]:
        raise ValueError("static stream declaration changed")
    fn = next(
        n
        for n in ast.parse(body).body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == declaration["function"]
    )
    assignments = {
        n.targets[0].id: n.value
        for n in ast.walk(fn)
        if isinstance(n, ast.Assign)
        and len(n.targets) == 1
        and isinstance(n.targets[0], ast.Name)
    }
    template = assignments[declaration["url"]]
    table_node = next(
        n.value
        for n in fn.body
        if isinstance(n, ast.Assign)
        and isinstance(n.targets[0], ast.Name)
        and n.targets[0].id == declaration["origins"]
        and isinstance(n.value, ast.Dict)
    )
    table = ast.literal_eval(table_node)
    # A source-literal mirror, the owning package and the approved object must
    # resolve to the same exact upstream GET URL in the pinned source.
    if not any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "stream"
        and len(n.args) == 2
        and isinstance(n.args[0], ast.Constant)
        and n.args[0].value == "GET"
        and isinstance(n.args[1], ast.Name)
        and n.args[1].id == declaration["url"]
        for n in ast.walk(fn)
    ):
        raise ValueError("missing upstream streaming GET")
    for base in sorted(set(table.values())):
        if base.rstrip("/") not in mirrors:
            continue
        bindings = {
            declaration["origin_parameter"]: base,
            declaration["package_parameter"]: package,
        }
        url = _constant_string(template, bindings)
        if url != base + package + "/" + declaration["object"]:
            raise ValueError("invalid static stream declaration")
        yield url, declaration["mime"], declaration["bytes"]


def from_snapshot(path, lock, *, stream_objects=None, policy=None):
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
                declaration = (policy or {}).get("stream_object", {})
                if stream_objects is not None and name == declaration.get(
                    "declaration"
                ):
                    stream_objects.extend(
                        _stream_objects(
                            body, declaration, policy["bulk_mirrors"], lock["package"]
                        )
                    )
                if b"https://" not in body and b"download_all_file" not in body:
                    continue
                tree = ast.parse(body)
                images.update(_static_table_images(tree))
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
    if (policy or {}).get("stream_object") and len(stream_objects) != len(
        policy["bulk_mirrors"]
    ):
        raise ValueError("incomplete source declaration for static stream")
    return sorted(images), sorted(directories)
