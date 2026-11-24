"""Verify the vendored GsCore TypedDict declarations and their worker-archive imports."""

import argparse
import ast
import hashlib
import importlib
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import typing
from unittest.mock import patch
import zipfile

from native_contract import REFERENCE_COMMIT, ROOT


PREFIX = "gsuid_core.utils.api.mys"
SCHEMA = "c2f982ed824d5b4660743e9963c8683401b23c1bd9ed9b4fce702af414f9d6e7"


def schema_files(directory):
    files = {
        p.name: p.read_bytes().replace(b"\r\n", b"\n")
        for p in sorted(directory.glob("*.py"))
    }
    manifest = "".join(
        f"{name} {hashlib.sha256(raw).hexdigest()}\n" for name, raw in files.items()
    )
    if hashlib.sha256(manifest.encode()).hexdigest() != SCHEMA:
        raise ValueError(f"native model tree checksum mismatch in {directory}")
    return files


def add_namespace(name):
    module = ModuleType(name)
    module.__path__ = []
    sys.modules[name] = module
    parent, _, child = name.rpartition(".")
    if parent:
        setattr(sys.modules[parent], child, module)
    return module


def inspect_models(modules):
    base = importlib.import_module(PREFIX + ".models")
    output = {}
    for module in modules:
        if module.startswith("_"):
            continue
        source = importlib.import_module(base.__name__ + "." + module)
        for name, model in vars(source).items():
            if typing.is_typeddict(model) and model.__module__ == source.__name__:
                output[module + "." + name] = {
                    "class": model.__name__,
                    "fields": [
                        (key, str(value))
                        for key, value in typing.get_type_hints(
                            model, include_extras=True
                        ).items()
                    ],
                    "required": sorted(model.__required_keys__),
                    "optional": sorted(model.__optional_keys__),
                    "total": model.__total__,
                }
    exports = [
        (name, getattr(base, name).__module__, getattr(base, name).__name__)
        for name in base.__all__
    ]
    return output, exports


def check(reference):
    native_dir = reference / "gsuid_core/utils/api/mys"
    vendored = ROOT / "worker/plugin_mys_schema/models"
    files = schema_files(native_dir / "models")
    product = schema_files(vendored)
    if product != files:
        raise AssertionError("schema sources differ")
    modules = [Path(name).stem for name in files]
    parent_ast = ast.parse((native_dir / "__init__.py").read_bytes())
    parent_names = [
        n.name
        for stmt in parent_ast.body
        if isinstance(stmt, ast.ImportFrom) and stmt.module == "models"
        for n in stmt.names
    ]
    with tempfile.TemporaryDirectory(prefix="mys-schema-") as tmp:
        archive = Path(tmp) / "worker.pyz"
        with zipfile.ZipFile(archive, "w") as output:
            for name in ("plugin_mys.py", "plugin_bindings.py"):
                output.write(ROOT / "worker" / name, name)
            for name, raw in product.items():
                output.writestr("plugin_mys_schema/models/" + name, raw)
        outputs = []
        for run in ("native", "worker"):
            with (
                patch.dict(sys.modules),
                patch.object(sys, "path", [str(archive), *sys.path]),
            ):
                for name in (
                    "gsuid_core",
                    "gsuid_core.utils",
                    "gsuid_core.utils.api",
                    "gsuid_core.utils.database",
                    "gsuid_core.utils.database.models",
                ):
                    add_namespace(name)
                if run == "native":
                    parent = add_namespace(PREFIX)
                    parent.__path__.append(str(native_dir))
                else:
                    from plugin_mys import install_mys

                    install_mys(add_namespace)
                    parent = sys.modules[PREFIX]
                    if any(
                        getattr(parent, name) is not getattr(parent.models, name)
                        for name in parent_names
                    ):
                        raise AssertionError("parent package re-exports differ")
                    if str(archive) not in parent.models.__file__:
                        raise AssertionError("schema did not load from worker archive")
                outputs.append(inspect_models(modules))
        if outputs[0] != outputs[1]:
            raise AssertionError("TypedDict metadata or package export mismatch")
    fields, exports = outputs[0]
    return {
        "reference": REFERENCE_COMMIT,
        "tree_sha256": SCHEMA,
        "modules": len(files),
        "type_names": len(fields),
        "annotated_fields": sum(len(row["fields"]) for row in fields.values()),
        "export_count": len(exports),
        "parent_exports": parent_names,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reference", type=Path, default=ROOT / ".sayu-local/reference"
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = json.dumps(check(args.reference), indent=2)
    if args.output:
        args.output.write_text(result + "\n", encoding="utf8")
    print(result)
