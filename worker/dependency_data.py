"""Read-only exports from the data providers bundled with private workers."""

import hashlib
from collections import namedtuple
from dataclasses import dataclass
from functools import cache
from importlib import import_module, metadata
from pathlib import Path
from types import MappingProxyType


# Both modules load their data from JSON inside starrail-damage-cal 4.6.0.
_PROVIDERS = {
    ("starrail_damage_cal.excel", "model"): "starrail_damage_cal.excel.model",
    ("starrail_damage_cal.map", "SR_MAP_PATH"): "starrail_damage_cal.map.SR_MAP_PATH",
}
_TABLE_DIGEST = "5baeceae9b6c9da00eac3c1c5c60f3c1840524d83820e70a4177e6d51dbbe158"


@cache
def _verify_tables():
    dist = metadata.distribution("starrail-damage-cal")
    if dist.version != "4.6.0":
        raise ValueError("Unexpected starrail-damage-cal version")
    root = Path(dist.locate_file("starrail_damage_cal")).resolve(strict=True)
    digest = hashlib.sha256()
    files = [*root.glob("excel/*.json"), *root.glob("map/data/*.json")]
    for file in sorted(files, key=lambda p: p.relative_to(root).as_posix()):
        if not file.resolve(strict=True).is_relative_to(root):
            raise ValueError("Data table outside the dependency package")
        digest.update(file.relative_to(root).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(file.read_bytes()).digest())
    if digest.hexdigest() != _TABLE_DIGEST:
        raise ValueError("Unexpected starrail-damage-cal data tables")


@cache
def _record_type(struct):
    return namedtuple(struct.__name__, struct.__struct_fields__)


def _readonly(value):
    if type(value) is dict:
        return MappingProxyType({k: _readonly(v) for k, v in value.items()})
    if type(value) is list:
        return tuple(_readonly(v) for v in value)
    if value is None or type(value) in (float, int, bool, str):
        return value
    struct = type(value)
    if struct.__module__ in _PROVIDERS.values() and hasattr(
        struct, "__struct_fields__"
    ):
        return _record_type(struct)(
            *(_readonly(getattr(value, k)) for k in struct.__struct_fields__)
        )
    raise TypeError("Unsupported dependency data value")


@cache
def _table(module, name):
    if name.startswith("_"):
        raise AttributeError(name)
    _verify_tables()
    value = vars(import_module(module)).get(name)
    if type(value) not in (dict, list):
        raise AttributeError(name)
    return _readonly(value)


@dataclass(frozen=True, slots=True)
class _DataModule:
    _module: str

    def __getattr__(self, name):
        return _table(self._module, name)


def dependency_import(module, symbol):
    """Resolve upstream data imports without exporting modules, paths or functions."""
    provider = _PROVIDERS.get((module, symbol))
    if provider:
        _verify_tables()
        return _DataModule(provider)
    if module in _PROVIDERS.values() and symbol:
        try:
            return _table(module, symbol)
        except AttributeError:
            pass
    raise KeyError((module, symbol))
