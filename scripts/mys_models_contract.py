"""Compare pinned type and endpoint declarations through native and archive imports."""

import argparse
import ast
import hashlib
import importlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import ModuleType
import typing
from unittest.mock import patch
import zipfile

from native_contract import REFERENCE_COMMIT, ROOT


PREFIX = "gsuid_core.utils.api.mys"
SCHEMA = "c2f982ed824d5b4660743e9963c8683401b23c1bd9ed9b4fce702af414f9d6e7"
ENDPOINT_SHA256 = "d3c3d0544f18da993eedae0182aea4e9476024d929831558f0f6223cb2cc9887"


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
                # TypedDict is a callable in plugin code. Required/optional fields
                # describe its static contract; at runtime it constructs a dict.
                first = next(iter(model.__annotations__), "fixture_field")
                specimen = model({first: "fixture-value"})
                output[module + "." + name] = {
                    "class": model.__name__,
                    "empty_value": model(),
                    "sample_value": specimen,
                    "runtime_type": type(specimen).__qualname__,
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


def check(reference, *, worker=None, worker_sha256=None):
    if (worker is None) != (worker_sha256 is None):
        raise ValueError("provide both the frozen worker and its SHA-256")
    native_dir = reference / "gsuid_core/utils/api/mys"
    vendored = ROOT / "worker/plugin_mys_schema/models"
    files = schema_files(native_dir / "models")
    product = schema_files(vendored)
    if product != files:
        raise AssertionError("schema sources differ")
    endpoint_source = (vendored.parent / "api.py").read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(endpoint_source).hexdigest() == ENDPOINT_SHA256
    assert endpoint_source == (native_dir / "api.py").read_bytes().replace(
        b"\r\n", b"\n"
    )
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
        if worker is None:
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("plugin_mys_schema/api.py", endpoint_source)
                for name in ("plugin_mys.py", "plugin_bindings.py"):
                    output.write(ROOT / "worker" / name, name)
                for name, raw in product.items():
                    output.writestr("plugin_mys_schema/models/" + name, raw)
        else:
            shutil.copyfile(worker, archive)
            with archive.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != worker_sha256:
                    raise ValueError("frozen worker digest mismatch")
            with zipfile.ZipFile(archive) as sealed:
                prefix = "plugin_mys_schema/models/"
                declarations = {
                    name.removeprefix(prefix): sealed.read(name).replace(b"\r\n", b"\n")
                    for name in sealed.namelist() if name.startswith(prefix)
                }
                assert declarations == files
                sealed_api = sealed.read("plugin_mys_schema/api.py")
                assert sealed_api.replace(b"\r\n", b"\n") == endpoint_source
        outputs = []
        endpoints = []
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
                api = importlib.import_module(PREFIX + ".api")
                assert parent.api is api and api.ApiEndpoint.__module__ == api.__name__
                if run == "worker":
                    assert str(archive) in api.__file__
                observations = {}
                for name, value in vars(api).items():
                    if not name.isupper():
                        continue
                    if isinstance(value, str):
                        observations[name] = value
                        continue
                    assert isinstance(value, api.ApiEndpoint)
                    regions = []
                    for overseas in (False, True):
                        try:
                            regions.append(value.get(overseas))
                        except ValueError as exc:
                            regions.append(("ValueError", str(exc)))
                    observations[name] = (
                        value.name,
                        value.has_cn,
                        value.has_os,
                        str(value),
                        regions,
                    )
                endpoints.append(observations)
                outputs.append(inspect_models(modules))
        if outputs[0] != outputs[1]:
            raise AssertionError("TypedDict metadata or package export mismatch")
        assert endpoints[0] == endpoints[1]
    fields, exports = outputs[0]
    return {
        "reference": REFERENCE_COMMIT,
        "frozen_worker_sha256": worker_sha256,
        "tree_sha256": SCHEMA,
        "modules": len(files),
        "type_names": len(fields),
        "annotated_fields": sum(len(row["fields"]) for row in fields.values()),
        "export_count": len(exports),
        "endpoint_module_constants": len(endpoints[0]),
        "parent_exports": parent_names,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reference", type=Path, default=ROOT / ".sayu-local/reference"
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--worker-sha256")
    args = parser.parse_args()
    result = json.dumps(
        check(args.reference, worker=args.worker, worker_sha256=args.worker_sha256),
        indent=2,
    )
    if args.output:
        args.output.write_text(result + "\n", encoding="utf8")
    print(result)
