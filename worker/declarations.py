"""Index and check the upstream declarations that can be linked in a chat worker."""

import __future__
import ast
import builtins
import dis
import importlib.util
from types import CodeType


from port_errors import Unsupported


BUILTINS = {
    name: getattr(builtins, name)
    for name in (
        "abs",
        "all",
        "any",
        "bool",
        "bytes",
        "dict",
        "enumerate",
        "float",
        "int",
        "isinstance",
        "len",
        "list",
        "max",
        "min",
        "range",
        "reversed",
        "next",
        "FileNotFoundError",
        "OSError",
        "round",
        "set",
        "sorted",
        "str",
        "sum",
        "tuple",
        "zip",
        "Exception",
        "ValueError",
        "KeyError",
        "TypeError",
        "divmod",
        "pow",
        "ord",
        "chr",
        "bin",
        "hex",
        "frozenset",
    )
}
TRIGGERS = {
    "on_fullmatch",
    "on_command",
    "on_prefix",
    "on_suffix",
    "on_regex",
    "on_keyword",
}
METHODS = {
    "text",
    "image",
    "node",
    "update",
    "send",
    "send_option",
    "info",
    "debug",
    "warning",
    "exception",
    "error",
    "open",
    "read",
    "exists",
    "is_file",
    "join",
    "format",
    "strip",
    "lstrip",
    "rstrip",
    "lower",
    "upper",
    "replace",
    "removeprefix",
    "removesuffix",
    "split",
    "rsplit",
    "splitlines",
    "startswith",
    "endswith",
    "isdigit",
    "isdecimal",
    "isalpha",
    "find",
    "rfind",
    "count",
    "get",
    "items",
    "keys",
    "values",
    "append",
    "extend",
    "convert",
    "load",
    "decode",
    "glob",
    "rglob",
    "write",
    "dumps",
    "setdefault",
    "unlink",
    "new",
    "Draw",
    "truetype",
    "rectangle",
    "rounded_rectangle",
    "crop",
    "paste",
    "resize",
    "alpha_composite",
    "getbbox",
    "getlength",
    "textlength",
    "merge",
    "invert",
    "fill",
    "deepcopy",
    "get_config",
    "match",
    "fullmatch",
    "search",
    "findall",
    "sub",
    "group",
    "loads",
}


def compile_declaration(node, path):
    return compile(
        ast.Module(body=[node], type_ignores=[]),
        str(path),
        "exec",
        flags=__future__.annotations.compiler_flag,
        dont_inherit=True,
    )


def required_names(code):
    for instruction in dis.get_instructions(code):
        if instruction.opname in {"LOAD_NAME", "LOAD_GLOBAL"}:
            yield instruction.argval
    for const in code.co_consts:
        if isinstance(const, CodeType):
            yield from required_names(const)


def validate(node, unit, *, owned=(), methods=()):
    forbidden = (
        ast.Nonlocal,
        ast.Delete,
        ast.Yield,
        ast.YieldFrom,
        ast.ClassDef,
    )
    for part in ast.walk(node):
        if isinstance(part, ast.Name) and part.id in {
            "__builtins__",
            "__import__",
            "__build_class__",
            "__annotations__",
        }:
            raise Unsupported("unsupported interpreter namespace")
        if isinstance(part, ast.While):
            # Finite text truncation and padding loops in upstream formatters.
            if not any(
                isinstance(test, ast.Call)
                and isinstance(test.func, ast.Name)
                and test.func.id == "len"
                for test in ast.walk(part.test)
            ):
                raise Unsupported("unsupported while loop")
        if isinstance(part, ast.Global):
            for name in part.names:
                binding = unit.bindings.get(name)
                if not isinstance(binding, (ast.Assign, ast.AnnAssign)):
                    raise Unsupported("unsupported global state")
                try:
                    ast.literal_eval(binding.value)
                except (TypeError, ValueError):
                    raise Unsupported("unsupported global state") from None
        if isinstance(part, forbidden):
            raise Unsupported("unsupported Python statement")
        if isinstance(part, ast.Attribute):
            if (
                (
                    not isinstance(part.ctx, ast.Load)
                    and not (
                        isinstance(part.value, ast.Name) and part.value.id in owned
                    )
                )
                or part.attr.startswith("_")
                or part.attr == "assets"
            ):
                raise Unsupported("unsupported attribute access")
        if isinstance(part, ast.Call):
            if isinstance(
                part.func, ast.Attribute
            ) and part.func.attr in METHODS | TRIGGERS | set(methods):
                continue
            if not isinstance(part.func, ast.Name):
                raise Unsupported("unsupported callable")


class Unit:
    def __init__(self, name, path, source):
        self.path = path
        self.tree = ast.parse(source, filename=str(path))
        self.namespace = {
            "__file__": str(path),
            "__name__": name,
            "__builtins__": BUILTINS,
            "__annotations__": {},
        }
        base = name if path.name == "__init__.py" else name.rpartition(".")[0]
        self.package = base
        self.local_imports = {}
        self.namespace["__builtins__"] = {**BUILTINS, "__import__": self.import_linked}
        self.bindings = {}
        self.updates = {}
        for node in self.tree.body:
            if isinstance(node, ast.ImportFrom):
                module = importlib.util.resolve_name(
                    "." * node.level + (node.module or ""), base
                )
                for alias in node.names:
                    self.bind(alias.asname or alias.name, (module, alias.name))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.bind(alias.asname or alias.name, (alias.name, None))
            elif isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                self.bind(node.name, node)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                if (
                    len(targets) == 1
                    and isinstance(targets[0], ast.Name)
                    and node.value
                ):
                    self.bind(targets[0].id, node)
            elif isinstance(node, ast.With):
                self.index_reader(node)
            elif isinstance(node, ast.Try):
                # Only extract plain table initialization with a diagnostic-only
                # missing-file fallback. Never move a read past a guard or check.
                plain = (
                    not node.orelse
                    and not node.finalbody
                    and all(
                        isinstance(statement, ast.With) or self.diagnostic(statement)
                        for statement in node.body
                    )
                    and all(
                        isinstance(handler.type, ast.Name)
                        and handler.type.id in {"FileNotFoundError", "OSError"}
                        and all(
                            self.diagnostic(statement) for statement in handler.body
                        )
                        for handler in node.handlers
                    )
                )
                for statement in node.body:
                    if isinstance(statement, ast.With):
                        self.index_reader(statement, guarded=not plain)

    def import_linked(self, name, globals=None, locals=None, fromlist=(), level=0):
        try:
            return self.local_imports[name, tuple(fromlist or ()), level]
        except KeyError:
            raise ImportError("local import was not linked") from None

    def diagnostic(self, statement):
        if isinstance(statement, ast.Pass):
            return True
        if not isinstance(statement, ast.Expr) or not isinstance(
            statement.value, ast.Call
        ):
            return False
        call = statement.value
        return (
            isinstance(call.func, ast.Attribute)
            and call.func.attr in {"info", "warning", "success", "error", "debug"}
            and isinstance(call.func.value, ast.Name)
            and self.bindings.get(call.func.value.id) == ("gsuid_core.logger", "logger")
            and not call.keywords
            and all(isinstance(arg, ast.Constant) for arg in call.args)
        )

    def index_reader(self, node, *, guarded=False):
        """Index a named resource assignment or collection update under a read context."""
        if len(node.items) != 1 or len(node.body) != 1:
            return
        item = node.items[0]
        call = item.context_expr
        if not (
            isinstance(call, ast.Call)
            and (
                (isinstance(call.func, ast.Name) and call.func.id == "open")
                or (isinstance(call.func, ast.Attribute) and call.func.attr == "open")
            )
            and isinstance(item.optional_vars, ast.Name)
        ):
            return
        statement = node.body[0]
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target = statement.targets[0]
        elif isinstance(statement, ast.AnnAssign):
            target = statement.target
        elif (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Attribute)
            and statement.value.func.attr in {"update", "extend"}
            and isinstance(statement.value.func.value, ast.Name)
        ):
            key = statement.value.func.value.id
            if guarded:
                self.bindings[key] = None
            else:
                self.updates.setdefault(key, []).append(node)
            return
        else:
            return
        if isinstance(target, ast.Name):
            self.bind(target.id, None if guarded else node)

    def bind(self, name, binding):
        # Ambiguous definitions (including overload sets) need explicit review.
        self.bindings[name] = None if name in self.bindings else binding


def conditional_calls(node):
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return set()
    guarded = {
        part.func
        for branch in ast.walk(node)
        if isinstance(branch, (ast.If, ast.Try))
        for statement in (*branch.body, *branch.orelse)
        for part in ast.walk(statement)
        if isinstance(part, ast.Call) and isinstance(part.func, ast.Name)
    }
    # A reference used unconditionally or as a value must be linked eagerly.
    eager = {
        part.id
        for part in ast.walk(node)
        if isinstance(part, ast.Name)
        and isinstance(part.ctx, ast.Load)
        and part not in guarded
    }
    return {part.id for part in guarded} - eager
