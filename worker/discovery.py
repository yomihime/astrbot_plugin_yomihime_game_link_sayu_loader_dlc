"""Discover chat registrations in pinned upstream source without starting the application.

Only link declarations whose dependencies are available in the worker. Package
initializers also contain migrations, servers and scheduled jobs, so importing
an entire initializer to find its decorators would run unrelated application code.
"""

import ast
import builtins
import copy
import hashlib
import importlib
import importlib.util
import json
import math
import re
import statistics
import textwrap
import typing
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from declarations import (
    BUILTINS,
    METHODS,
    TRIGGERS,
    Unit,
    Unsupported,
    compile_declaration,
    conditional_calls,
    required_names,
    validate,
)
from dependency_data import dependency_import
from public_resources import PublicResources
from source_assets import Assets, AssetPath


PORTS = {
    "gsuid_core.sv": {"SV", "get_plugin_available_prefix"},
    "gsuid_core.pool": {"to_thread"},
    "gsuid_core.utils.plugins_config.gs_config": {"StringConfig"},
    "gsuid_core.utils.plugins_config.models": {
        "GsBoolConfig",
        "GsImageConfig",
        "GsIntConfig",
        "GsListStrConfig",
        "GsStrConfig",
    },
    "gsuid_core.bot": {"Bot"},
    "gsuid_core.models": {"Event", "Message"},
    "gsuid_core.segment": {"MessageSegment"},
    "gsuid_core.message_models": {"Button", "Message"},
    "gsuid_core.logger": {"logger"},
    "gsuid_core.i18n": {"t"},
    "gsuid_core.help.draw_new_plugin_help": {"get_new_help"},
    "gsuid_core.utils.image.convert": {"convert_img"},
}
# These two upstream helpers need native dependency initialization or overloads.
# Pin their reviewed source separately from the generic Python declaration linker.
NATIVE_MODULES = {
    "StarRailUID.starrailuid_alias.alias_manager": (
        "32b2a37041b414c7dc061f587428356ac87a08c9249ea7fc154c03e8b9646213",
        {
            "resolve_char_id",
            "get_char_id_by_alias",
            "get_canonical_name",
            "get_alias_list",
            "is_builtin_alias",
        },
    ),
    "GenshinUID.utils.image.convert": (
        "8cdfc5fea29e1fb4004c84653efb11688bf687ea77992858ec8727dcef318920",
        {"convert_img"},
    ),
}


class Deferred:
    """A guarded call still goes through normal capability and dependency checks."""

    def __init__(self, discovery, module, name):
        self.discovery = discovery
        self.module = module
        self.name = name

    def __call__(self, *args, **kwargs):
        return self.discovery.resolve(self.module, self.name)(*args, **kwargs)


class Discovery:
    def __init__(self, source, plugin):
        self.assets = Assets(source)
        root = source / plugin
        self.paths = {}
        if not plugin.isidentifier() or not root.is_dir():
            raise ValueError("invalid plugin source")
        for path in sorted(root.rglob("*.py")):
            parts = list(path.relative_to(source).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts.pop()
            self.paths[".".join(parts)] = path
        self.units = {}
        self.native = {}
        self.pending = set()
        self.model_methods = set()
        for name, path in self.paths.items():
            if b"BaseModel" in path.read_bytes():
                unit = self.unit(name)
                for definition in unit.tree.body:
                    if isinstance(definition, ast.ClassDef) and self.model_class(
                        unit, definition
                    ):
                        self.model_methods.update(
                            method.name
                            for method in definition.body
                            if isinstance(method, ast.FunctionDef)
                            and not method.name.startswith("_")
                        )

    def unit(self, name):
        if name not in self.units:
            path = self.paths[name]
            self.units[name] = Unit(name, path, path.read_text(encoding="utf-8-sig"))
        return self.units[name]

    def native_symbol(self, module, name):
        digest, exports = NATIVE_MODULES[module]
        if name not in exports:
            raise Unsupported("unavailable native helper")
        if module not in self.native:
            path = self.paths[module]
            source = path.read_text(encoding="utf-8")
            if hashlib.sha256(source.encode()).hexdigest() != digest:
                raise Unsupported("native helper digest mismatch")
            unit = self.unit(module)
            tree = copy.copy(unit.tree)
            namespace = {"__file__": str(path), "__name__": module}
            tree.body = []
            for node in unit.tree.body:
                if isinstance(node, ast.ImportFrom) and (
                    node.level
                    or (node.module or "").partition(".")[0] == "starrail_damage_cal"
                ):
                    for alias in node.names:
                        local = alias.asname or alias.name
                        target, symbol = unit.bindings[local]
                        value = self.resolve(target, symbol)
                        # Native helpers receive a real Path for their own file API.
                        namespace[local] = (
                            value._path if isinstance(value, AssetPath) else value
                        )
                else:
                    tree.body.append(node)
            exec(compile(tree, str(path), "exec"), namespace)
            self.native[module] = namespace
        function = self.native[module][name]
        if module == "GenshinUID.utils.image.convert":

            async def convert(image, *args, **kwargs):
                from PIL.Image import Image

                if not isinstance(image, (bytes, Image)):
                    image = self.assets.file(image)
                return await function(image, *args, **kwargs)

            return convert
        return function

    def data_path(self, name=None):
        from gsuid_core.data_store import get_res_path

        root = get_res_path()
        self.assets.data_root = root.resolve()
        return self.assets.path(get_res_path(name) if name else root)

    def external(self, module, name):
        if (module, name) == ("gsuid_core.data_store", "get_res_path"):
            return self.data_path
        if name in PORTS.get(module, ()):
            return getattr(importlib.import_module(module), name)
        pure_math = {
            "math": (
                math,
                {"ceil", "floor", "sqrt", "log", "log2", "pi", "e", "isclose"},
            ),
            "statistics": (statistics, {"mean", "median", "stdev"}),
        }
        if module in pure_math and name in pure_math[module][1]:
            return getattr(pure_math[module][0], name)
        if module == "pydantic" and name in {"BaseModel", "Field"}:
            return getattr(importlib.import_module(module), name)
        if (module, name) == ("copy", None):
            return SimpleNamespace(deepcopy=copy.deepcopy)
        if module == "typing" and name in {
            "Any",
            "Dict",
            "List",
            "Tuple",
            "Optional",
            "Literal",
            "Iterable",
            "Set",
            "Union",
        }:
            return getattr(typing, name)
        if (module, name) == ("msgspec", "json"):
            from msgspec import json as msgjson

            return SimpleNamespace(decode=msgjson.decode)
        if (module, name) == ("pathlib", "Path"):
            return self.assets.path
        if module == "PIL" and name in {"Image", "ImageDraw", "ImageFont", "ImageOps"}:
            from PIL import Image, ImageDraw, ImageOps
            from sayu_compat import MAX_INPUT_PIXELS

            def new(mode, size, color=0):
                if (
                    len(size) != 2
                    or min(size) < 1
                    or size[0] * size[1] > MAX_INPUT_PIXELS
                ):
                    raise ValueError("canvas outside supported dimensions")
                return Image.new(mode, size, color)

            return {
                "Image": SimpleNamespace(
                    open=self.assets.image,
                    new=new,
                    merge=Image.merge,
                    Image=Image.Image,
                ),
                "ImageDraw": SimpleNamespace(Draw=ImageDraw.Draw),
                "ImageFont": SimpleNamespace(truetype=self.assets.font),
                "ImageOps": SimpleNamespace(invert=ImageOps.invert),
            }[name]
        if (module, name) == ("textwrap", None):
            return SimpleNamespace(fill=textwrap.fill)
        if (module, name) == ("gsuid_core.utils.image.image_tools", "crop_center_img"):
            from PIL import ImageOps
            from sayu_compat import MAX_INPUT_PIXELS

            def crop_center_img(image, width, height):
                if min(width, height) < 1 or width * height > MAX_INPUT_PIXELS:
                    raise ValueError("crop outside supported dimensions")
                return ImageOps.fit(image, (width, height))

            return crop_center_img
        if (module, name) == ("aiofiles", None):
            return SimpleNamespace(open=self.assets.open_async)
        if (module, name) == ("json", None):
            return SimpleNamespace(load=json.load, loads=json.loads, dumps=json.dumps)
        if (module, name) == ("re", None):
            return SimpleNamespace(
                **{key: getattr(re, key) for key in METHODS if hasattr(re, key)}
            )
        try:
            return dependency_import(module, name)
        except KeyError:
            raise Unsupported("unsupported import") from None

    @staticmethod
    def model_class(unit, node):
        return (
            len(node.bases) == 1
            and isinstance(node.bases[0], ast.Name)
            and unit.bindings.get(node.bases[0].id) == ("pydantic", "BaseModel")
        )

    def data_class(self, module, node):
        unit = self.unit(module)
        if node.decorator_list or node.keywords:
            raise Unsupported("unsupported class declaration")
        for item in node.body:
            if isinstance(item, ast.FunctionDef) and not item.decorator_list:
                if item.name.startswith("__") and item.name != "__init__":
                    raise Unsupported("unsupported data model protocol")
                validate(item, unit, owned={"self"}, methods=self.model_methods)
            elif isinstance(item, ast.ClassDef) and item.name == "Config":
                if item.bases or item.decorator_list or item.keywords:
                    raise Unsupported("unsupported model configuration")
                for field in item.body:
                    if not isinstance(field, ast.Assign):
                        raise Unsupported("unsupported model configuration")
                    try:
                        ast.literal_eval(field.value)
                    except (TypeError, ValueError):
                        raise Unsupported("unsupported model configuration") from None
            elif isinstance(item, (ast.AnnAssign, ast.Assign, ast.Expr, ast.Pass)):
                validate(item, unit, methods=self.model_methods)
            else:
                raise Unsupported("unsupported data model declaration")
        # Field types must be evaluated in the source module for Pydantic to
        # validate nested models. These classes have no arbitrary base/metaclass.
        code = compile(
            ast.Module(body=[node], type_ignores=[]),
            str(unit.path),
            "exec",
            dont_inherit=True,
        )
        unit.namespace["__builtins__"]["__build_class__"] = builtins.__build_class__
        for name in sorted(
            set(required_names(code)) - unit.namespace.keys() - BUILTINS.keys()
        ):
            unit.namespace[name] = self.resolve(module, name)
        exec(code, unit.namespace)
        return unit.namespace[node.name]

    def local_function(self, unit, name):
        binding = unit.bindings.get(name)
        if isinstance(binding, tuple):
            target, symbol = binding
            if target not in self.paths:
                return target.startswith("gsuid_core.")
            binding = self.unit(target).bindings.get(symbol)
        return isinstance(binding, (ast.FunctionDef, ast.AsyncFunctionDef))

    def resolve(self, module, name):
        child = f"{module}.{name}"
        if child in self.paths and (
            module not in self.paths or name not in self.unit(module).bindings
        ):

            class LinkedModule:
                def __getattr__(_, symbol):
                    if symbol.startswith("_"):
                        raise AttributeError(symbol)
                    return self.resolve(child, symbol)

            return LinkedModule()
        if module in NATIVE_MODULES:
            return self.native_symbol(module, name)
        if module not in self.paths:
            return self.external(module, name)
        unit = self.unit(module)
        cached = unit.namespace.get(name)
        if name in unit.namespace and not isinstance(cached, Deferred):
            return cached
        binding = unit.bindings.get(name)
        key = (module, name)
        if not binding or key in self.pending:
            raise Unsupported("missing, ambiguous or cyclic binding")
        self.pending.add(key)
        try:
            if isinstance(binding, tuple):
                value = self.resolve(*binding)
            elif isinstance(binding, ast.ClassDef) and (
                self.model_class(unit, binding) or not binding.bases
            ):
                value = self.data_class(module, binding)
            elif isinstance(binding, ast.ClassDef):
                # Upstream button subclasses attach a game-specific prefix.
                if (
                    len(binding.bases) != 1
                    or not isinstance(binding.bases[0], ast.Name)
                    or binding.decorator_list
                    or binding.keywords
                    or len(binding.body) != 1
                ):
                    raise Unsupported("unsupported class")
                base = self.resolve(module, binding.bases[0].id)
                field = binding.body[0]
                if (
                    base is not self.external("gsuid_core.message_models", "Button")
                    or not isinstance(field, ast.Assign)
                    or len(field.targets) != 1
                    or not isinstance(field.targets[0], ast.Name)
                    or field.targets[0].id != "prefix"
                    or not isinstance(field.value, ast.Name)
                ):
                    raise Unsupported("unsupported class")
                value = type(
                    name, (base,), {"prefix": self.resolve(module, field.value.id)}
                )
            else:
                if (
                    isinstance(binding, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and binding.decorator_list
                ):
                    # Only a reviewed transparent worker adapter may wrap linked helpers.
                    if not all(
                        isinstance(d, ast.Name)
                        and unit.bindings.get(d.id) == ("gsuid_core.pool", "to_thread")
                        for d in binding.decorator_list
                    ):
                        raise Unsupported("decorated helper")
                value = self.execute(module, binding)[name]
            unit.namespace[name] = value
            for update in unit.updates.get(name, ()):
                self.execute(module, update)
            return value
        except Exception:
            # The initial empty table must never satisfy a subsequent consumer
            # when opening or validating its packaged resource failed.
            if isinstance(cached, Deferred):
                unit.namespace[name] = cached
            else:
                unit.namespace.pop(name, None)
            raise
        finally:
            self.pending.remove(key)

    def link_local_imports(self, unit, node):
        # Local imports use exactly the same dependency boundary as module globals.
        for item in ast.walk(node):
            if isinstance(item, ast.ImportFrom):
                names = tuple(alias.name for alias in item.names)
                module = importlib.util.resolve_name(
                    "." * item.level + (item.module or ""), unit.package
                )
                values = {name: self.resolve(module, name) for name in names}
                unit.local_imports[item.module or "", names, item.level] = (
                    SimpleNamespace(**values)
                )
            elif isinstance(item, ast.Import):
                for alias in item.names:
                    if "." in alias.name:
                        raise Unsupported("unsupported local package import")
                    unit.local_imports[alias.name, (), 0] = self.resolve(
                        alias.name, None
                    )

    def execute(self, module, node):
        unit = self.unit(module)
        owned = {
            part.targets[0].id
            for part in ast.walk(node)
            if isinstance(part, ast.Assign)
            and len(part.targets) == 1
            and isinstance(part.targets[0], ast.Name)
            and isinstance(part.value, ast.Call)
            and isinstance(part.value.func, ast.Name)
            and isinstance(unit.bindings.get(part.value.func.id), ast.ClassDef)
        }
        validate(node, unit, owned=owned, methods=self.model_methods)
        self.link_local_imports(unit, node)
        private_opens = set()
        private_writes = set()
        for block in ast.walk(node):
            if (
                not isinstance(block, ast.With)
                or len(block.items) != 1
                or len(block.body) != 1
            ):
                continue
            item = block.items[0]
            opener = item.context_expr
            stmt = block.body[0]
            if not (
                isinstance(opener, ast.Call)
                and isinstance(opener.func, ast.Name)
                and opener.func.id == "open"
                and len(opener.args) == 2
                and isinstance(opener.args[0], ast.Name)
                and isinstance(opener.args[1], ast.Constant)
                and opener.args[1].value == "w"
                and isinstance(item.optional_vars, ast.Name)
                and isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Call)
            ):
                continue
            write = stmt.value
            if not (
                isinstance(write.func, ast.Attribute)
                and write.func.attr == "write"
                and isinstance(write.func.value, ast.Name)
                and write.func.value.id == item.optional_vars.id
                and len(write.args) == 1
                and isinstance(write.args[0], ast.Call)
            ):
                continue
            dumps = write.args[0].func
            if not (
                isinstance(dumps, ast.Attribute)
                and dumps.attr == "dumps"
                and isinstance(dumps.value, ast.Name)
                and unit.bindings.get(dumps.value.id) == ("json", None)
            ):
                continue
            destination = self.resolve(module, opener.args[0].id)
            if (
                not isinstance(destination, AssetPath)
                or self.assets.data_root is None
                or not destination._path.is_relative_to(self.assets.data_root)
                or destination._path.suffix != ".json"
                or "resource"
                in destination._path.relative_to(self.assets.data_root).parts
            ):
                raise Unsupported("unsupported JSON destination")
            self.assets.private_json.add(destination._path)
            private_opens.add(opener)
            private_writes.add(write)
        for call in (part for part in ast.walk(node) if isinstance(part, ast.Call)):
            method = call.func
            if call in private_opens:
                continue
            if (
                isinstance(method, ast.Attribute)
                and method.attr == "write"
                and call not in private_writes
            ):
                raise Unsupported("unsupported write")
            if isinstance(method, ast.Name) and method.id == "open":
                mode_index = 1
            elif isinstance(method, ast.Attribute) and method.attr == "open":
                if not isinstance(method.value, ast.Name):
                    raise Unsupported("unreviewed file reader")
                owner = method.value.id
                if unit.bindings.get(owner) in {("aiofiles", None), ("PIL", "Image")}:
                    mode_index = 1
                elif owner in unit.bindings and isinstance(
                    self.resolve(module, owner), AssetPath
                ):
                    mode_index = 0
                else:
                    raise Unsupported("unreviewed file reader")
            else:
                continue
            modes = [
                *call.args[mode_index : mode_index + 1],
                *(kw.value for kw in call.keywords if kw.arg == "mode"),
            ]
            if any(
                not isinstance(mode, ast.Constant) or mode.value not in {"r", "rb"}
                for mode in modes
            ):
                raise Unsupported("unsupported file mode")
        unit.namespace["open"] = self.assets.open
        code = compile_declaration(node, unit.path)
        dependencies = {
            name
            for name in required_names(code)
            if name not in BUILTINS
            and (
                name not in unit.namespace or isinstance(unit.namespace[name], Deferred)
            )
        }
        if isinstance(node, ast.With):
            dependencies.discard(node.items[0].optional_vars.id)
        conditional = conditional_calls(node)
        for name in sorted(dependencies):
            if name in conditional and self.local_function(unit, name):
                unit.namespace[name] = Deferred(self, module, name)
            else:
                unit.namespace[name] = self.resolve(module, name)
        # A context manager's local file variable should not survive this declaration.
        namespace = (
            dict(unit.namespace) if isinstance(node, ast.With) else unit.namespace
        )
        exec(code, namespace)
        return namespace

    def register(self, module, node):
        from gsuid_core.sv import SV

        unit = self.unit(module)
        for decorator in node.decorator_list:
            if not (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr in TRIGGERS
                and isinstance(decorator.func.value, ast.Name)
            ):
                raise Unsupported("unsupported decorator")
            service = self.resolve(module, decorator.func.value.id)
            if not isinstance(service, SV):
                raise Unsupported("not a service")
            if service.pm < 6 or not service.enabled:
                raise Unsupported("restricted service")
        self.execute(module, node)
        return unit.namespace[node.name]


async def discover(
    source: Path, plugin: str, *, skipped: Counter | None = None, discovery=None
) -> tuple:
    """Register available upstream handlers. Incomplete dependency graphs stay inactive."""
    from sayu_compat import registration_scope

    if discovery is None:
        discovery = Discovery(Path(source).resolve(), plugin)
    callbacks = []
    candidates = []
    for module, path in discovery.paths.items():
        source_bytes = path.read_bytes()
        if b".on_" in source_bytes:
            candidates.append(module)
        if b"on_core_start" not in source_bytes:
            continue
        unit = discovery.unit(module)
        for hook in unit.tree.body:
            if not (
                isinstance(hook, ast.AsyncFunctionDef) and len(hook.decorator_list) == 1
            ):
                continue
            marker = hook.decorator_list[0]
            if not (
                isinstance(marker, ast.Name)
                and unit.bindings.get(marker.id)
                == ("gsuid_core.server", "on_core_start")
            ):
                continue
            caches = {
                item.func.value.id
                for item in ast.walk(hook)
                if isinstance(item, ast.Call)
                and isinstance(item.func, ast.Attribute)
                and item.func.attr in {"update", "extend", "append"}
                and isinstance(item.func.value, ast.Name)
                and item.func.value.id in unit.bindings
            }
            if not caches:
                continue
            # Only hydrate in-memory tables from the current package's assets.
            implementation = copy.copy(hook)
            implementation.decorator_list = []
            try:
                discovery.execute(module, implementation)
                await unit.namespace[hook.name]()
            except (Unsupported, ImportError, AttributeError, OSError, ValueError):
                for name in caches:
                    unit.namespace.pop(name, None)
                    unit.bindings[name] = None
    for module in candidates:
        unit = discovery.unit(module)
        for node in unit.tree.body:
            if isinstance(node, ast.AsyncFunctionDef) and node.decorator_list:
                try:
                    with registration_scope():
                        callbacks.append(discovery.register(module, node))
                except (
                    Unsupported,
                    ImportError,
                    AttributeError,
                    OSError,
                    KeyError,
                    ValueError,
                    TypeError,
                ) as error:
                    if skipped is not None:
                        skipped[
                            str(error)
                            if isinstance(error, Unsupported)
                            else type(error).__name__
                        ] += 1
    # A startup scan may inspect many unrelated declarations. Only a live
    # callback may hydrate a missing file from a declared public resource root.
    public = PublicResources(Path(source), plugin)
    discovery.data_path()
    public.bind(discovery.resolve, discovery.assets.data_root)
    discovery.assets.public = public
    if plugin == "XutheringWavesUID":
        from resource_index import ResourceIndex

        discovery.assets.index_root = discovery.assets.data_root / plugin / "resource"
        discovery.assets.index = ResourceIndex(plugin, discovery.assets.index_root)
    return tuple(callbacks)
