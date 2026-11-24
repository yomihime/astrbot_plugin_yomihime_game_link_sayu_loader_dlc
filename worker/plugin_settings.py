"""Plugin settings backed by the pinned GsCore tagged msgspec schema.

plugin_config_models.py comes from gsuid_core/utils/plugins_config/models.py,
commit 96cd7353be3100ff982bccc33e554961837c778e, GPL-3.0. The worker ships
licenses/gsuid_core-COPYING.txt. Only plugin-owned configuration is registered.
"""

from copy import deepcopy
from datetime import date
import hashlib
import os
from pathlib import Path, PureWindowsPath
import pkgutil
import shutil
import tempfile
from threading import RLock
from types import MappingProxyType
from typing import get_args

import msgspec
import plugin_config_models as schema
from port_errors import Unsupported
from service_settings import _grant
from worker_storage import publish_new_config as _publish_new

SCHEMA_SHA256 = "861bf9f73e650482f968a3e5343140a3dcd199c84572cd0b6883326ef676e833"
MODELS = {t.__name__: t for t in get_args(schema.GSC)}
MODELS.update(
    {
        k: getattr(schema, k)
        for k in (
            "GSC",
            "GsConfig",
            "ButtonState",
            "ButtonStatusField",
            "ButtonStateView",
            "button_state_view",
        )
    }
)
_RAW = dict[str, schema.GSC]


def _writable():
    grant = _grant.get()
    return bool(grant and grant.writable)


def _check_write():
    if not _writable():
        raise Unsupported(
            "configuration mutation requires plugin management permissions"
        )


def reconcile_config(stored, default, *, config_name=""):
    output = {}
    for key, prototype in default.items():
        value = stored.get(key)
        if type(prototype) is not type(value):
            output[key] = deepcopy(prototype)
            continue
        value.title, value.desc = prototype.title, prototype.desc
        if hasattr(prototype, "secret"):
            value.secret = prototype.secret
        if isinstance(
            value, (schema.GsIntConfig, schema.GsStrConfig, schema.GsListStrConfig)
        ):
            value.options = deepcopy(prototype.options)
        if isinstance(value, schema.GsButtonConfig):
            for attr in (
                "endpoint",
                "label",
                "level",
                "confirm",
                "status_fields",
                "show_progress",
                "poll_ms",
            ):
                setattr(value, attr, deepcopy(getattr(prototype, attr)))
        if isinstance(value, schema.GsRepeatGroupConfig):
            value.template = deepcopy(prototype.template)
            value.data = [
                reconcile_config(row, prototype.template, config_name=config_name)
                for row in value.data
            ]
        output[key] = value
    output.update((k, v) for k, v in stored.items() if k not in default)
    return output


def _group(raw, template):
    group = deepcopy(template)
    for key, item in group.items():
        if isinstance(item, schema.GsRepeatGroupConfig):
            values = raw.get(key)
            item.data = (
                [_group(v, item.template) for v in values if isinstance(v, dict)]
                if isinstance(values, list)
                else []
            )
        elif key in raw and not isinstance(item, schema.GsDivider):
            item.data = raw[key]
    return group


def _repair(saved, defaults):
    fixed = {}
    for key, value in saved.items():
        replacement = defaults.get(key)
        try:
            typed = msgspec.convert(value, schema.GSC)
            if replacement is not None and list(value) != list(
                msgspec.to_builtins(replacement)
            ):
                typed = deepcopy(replacement)
        except msgspec.ValidationError:
            if replacement is None:
                continue
            typed = deepcopy(replacement)
        fixed[key] = typed
    return fixed


def _assign(item, value):
    if isinstance(item, schema.GsRepeatGroupConfig) and isinstance(value, list):
        item.data = [
            _group(row, item.template) for row in value if isinstance(row, dict)
        ]
    elif type(item.data) is type(value):
        item.data = value
    elif isinstance(item, schema.GsTimeRConfig) and isinstance(value, list):
        item.data = tuple(value)
    elif (
        isinstance(item, schema.GsTimeRangeConfig)
        and isinstance(value, (list, tuple))
        and len(value) == 2
    ):
        try:
            start, end = value
            item.data = ((int(start[0]), int(start[1])), (int(end[0]), int(end[1])))
        except (TypeError, IndexError, ValueError):
            return False
    elif isinstance(item, schema.GsFloatConfig) and type(value) is int:
        item.data = float(value)
    elif isinstance(item, schema.GsDateConfig) and isinstance(value, str):
        try:
            item.data = date.fromisoformat(value)
        except ValueError:
            return False
    else:
        return False
    return True


def configuration_type(root, plugin=None, *, registry=None):
    root = Path(root).resolve()
    instances = {} if registry is None else registry
    lock = RLock()

    def checked(path):
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(root) or resolved == root:
            raise ValueError("configuration path outside plugin data")
        if resolved.exists() and (
            not resolved.is_file() or resolved.stat().st_nlink != 1
        ):
            raise ValueError("configuration path must be a private file")
        return resolved

    def persist(path, values):
        _check_write()
        path = checked(path)
        content = msgspec.json.format(msgspec.json.encode(values), indent=4)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
            pending = Path(file.name)
        try:
            pending.write_bytes(content)
            os.replace(pending, path)
        finally:
            pending.unlink(missing_ok=True)

    def migrate_file(old, final):
        _check_write()
        final.parent.mkdir(exist_ok=True, parents=True)
        try:
            content = old.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return
        with tempfile.NamedTemporaryFile(dir=final.parent, delete=False) as file:
            pending = Path(file.name)
        try:
            pending.write_text(content, encoding="utf-8")
            if _publish_new(pending, final):
                old.unlink()
        finally:
            pending.unlink(missing_ok=True)

    class StringConfig:
        def __new__(cls, config_name, CONFIG_PATH, config_list):
            if config_name is None:
                raise ValueError("Config.name is None!")
            with lock:
                if _writable() and config_name in instances:
                    return instances[config_name]
                return super().__new__(cls)

        def __init__(self, config_name, CONFIG_PATH, config_list):
            with lock:
                paths = CONFIG_PATH if isinstance(CONFIG_PATH, list) else [CONFIG_PATH]
                *previous, final = map(checked, paths)
                if not final.exists():
                    for old in previous:
                        if old.is_file():
                            migrate_file(old, final)
                            break
                self.CONFIG_PATH = final
                self.config_name = config_name
                self.plugin_name = plugin
                self._defaults = deepcopy(config_list)
                self.update_config()
                if _writable():
                    instances[config_name] = self

        @property
        def config_list(self):
            return self._defaults if _writable() else deepcopy(self._defaults)

        config_default = config_list

        @property
        def config(self):
            return self._values if _writable() else deepcopy(self._values)

        def _read(self):
            path = checked(self.CONFIG_PATH)
            if not path.exists():
                return {}
            try:
                text = path.read_text("utf-8")
            except UnicodeDecodeError:
                text = path.read_text()
            try:
                return msgspec.json.decode(text, type=_RAW)
            except msgspec.ValidationError:
                return _repair(msgspec.json.decode(text), self._defaults)

        def update_config(self):
            with lock:
                values = reconcile_config(
                    self._read(), self._defaults, config_name=self.config_name
                )
                if _writable():
                    persist(self.CONFIG_PATH, values)
                self._values = values

        def repair_config(self):
            _check_write()
            with lock:
                raw = msgspec.json.decode(checked(self.CONFIG_PATH).read_bytes())
                persist(self.CONFIG_PATH, _repair(raw, self._defaults))

        def sort_config(self):
            _check_write()
            with lock:
                values = {
                    k: self._values[k] for k in self._defaults if k in self._values
                }
                values.update(self._values)
                persist(self.CONFIG_PATH, values)
                self._values = values

        def write_config(self):
            with lock:
                persist(self.CONFIG_PATH, self._values)

        def __iter__(self):
            return iter(self._values)

        def __len__(self):
            return len(self._values)

        def __getitem__(self, key):
            item = self._values[key]
            return item if _writable() else deepcopy(item)

        def get_raw_config(self):
            return msgspec.to_builtins(self._values)

        def get_config(self, key, default_value=None):
            if key not in self._values and key in self._defaults:
                self.update_config()
            if key in self._values:
                return self[key]
            cls = {
                str: schema.GsStrConfig,
                bool: schema.GsBoolConfig,
                list: schema.GsListStrConfig,
                dict: schema.GsDictConfig,
            }.get(type(default_value))
            if cls is None:
                cls, default_value = schema.GsBoolConfig, False
            return cls("缺省值", "获取错误的配置项", default_value)

        def set_config(self, key, value):
            _check_write()
            with lock:
                if key not in self._defaults:
                    return False
                item = self._values[key]
                if isinstance(item, schema.GsButtonConfig):
                    return True
                updated = deepcopy(item)
                if not _assign(updated, value):
                    return False
                self._write_value(key, updated.data)
                return True

        def _write_value(self, key, value):
            item = self._values[key]
            previous, item.data = item.data, value
            try:
                self.write_config()
            except BaseException:
                item.data = previous
                raise

        def set_button_state(self, key, state):
            _check_write()
            with lock:
                if not isinstance(self._values.get(key), schema.GsButtonConfig):
                    return False
                self._write_value(key, state)
                return True

        def migrate_from(self, old_configs):
            _check_write()
            old_configs = (
                old_configs if isinstance(old_configs, list) else [old_configs]
            )
            with lock:
                for old in old_configs:
                    if (
                        not isinstance(old, StringConfig)
                        or old.CONFIG_PATH == self.CONFIG_PATH
                    ):
                        raise ValueError(
                            "expected another configuration in the same profile"
                        )
                    moved = self._defaults.keys() & old._values.keys()
                    if not moved:
                        continue
                    new_values = deepcopy(self._values)
                    for key in moved:
                        new_values[key].data = deepcopy(old._values[key].data)
                    # Publish the destination first, so an interrupted source cleanup can be retried.
                    persist(self.CONFIG_PATH, new_values)
                    self._values = new_values
                    remaining = {k: v for k, v in old._values.items() if k not in moved}
                    persist(old.CONFIG_PATH, remaining)
                    old._values = remaining

    return StringConfig


def configuration_manager_type(root, string_config):
    root = Path(root).resolve()
    lock = RLock()

    class ConfigSetManager:
        def __init__(self, base_path, config_template, name_suffix=""):
            self._base_path = Path(base_path).resolve()
            if not self._base_path.is_relative_to(root):
                raise ValueError("configuration set outside private profile")
            self._config_template = config_template
            self._name_suffix = name_suffix
            self._cache = {}

        def _get_config_path(self, config_name):
            return self._base_path / f"{config_name}.json"

        def _file(self, name):
            if (
                not name
                or name in {".", ".."}
                or any(char in name for char in "/\\:\0")
                or (os.name == "nt" and PureWindowsPath(f"{name}.json").is_reserved())
            ):
                raise ValueError("configuration name must be a single path component")
            base = self._base_path.resolve()
            path = Path(self._get_config_path(name)).resolve()
            if (
                not base.is_relative_to(root)
                or path == base
                or not path.is_relative_to(base)
            ):
                raise ValueError("configuration outside managed directory")
            if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
                raise ValueError("configuration path must be a private file")
            return path

        def _list_configs(self):
            base = self._base_path.resolve()
            if not base.is_relative_to(root):
                raise ValueError("configuration set outside private profile")
            if not base.exists():
                return []
            return sorted(
                path.stem
                for path in base.iterdir()
                if path.suffix == ".json"
                and path.is_file()
                and self._file(path.stem).is_file()
            )

        def _create_string_config(self, config_name):
            path = self._file(config_name)
            if _writable():
                path.parent.mkdir(parents=True, exist_ok=True)
            return string_config(
                f"{type(self).__name__}{self._name_suffix}_{config_name}",
                path,
                self._config_template.copy(),
            )

        def get_config(self, config_name):
            with lock:
                self._file(config_name)
                if config_name in self._cache:
                    return self._cache[config_name]
                item = self._create_string_config(config_name)
                if _writable():
                    self._cache[config_name] = item
                return item

        def list_available(self):
            return self._list_configs()

        def get_all_configs(self):
            return {name: self.get_config(name) for name in self.list_available()}

        def exists(self, config_name):
            return self._file(config_name).is_file()

        def copy(self, source_name, new_name):
            _check_write()
            source_name = (source_name or "").strip()
            new_name = (new_name or "").strip()
            if not new_name:
                return "名称不能为空"
            if "+" in new_name:
                return "配置文件名称不允许包含 '+' 字符"
            if not source_name or source_name == new_name:
                return "新名称与原配置相同"
            with lock:
                source = self._file(source_name)
                if not source.is_file():
                    return f"配置文件 '{source_name}' 不存在"
                dest = self._file(new_name)
                if dest.exists():
                    return f"配置文件 '{new_name}' 已存在"
                dest.parent.mkdir(parents=True, exist_ok=True)
                pending = None
                try:
                    with (
                        source.open("rb") as incoming,
                        tempfile.NamedTemporaryFile(
                            dir=dest.parent, delete=False
                        ) as outgoing,
                    ):
                        pending = Path(outgoing.name)
                        shutil.copyfileobj(incoming, outgoing)
                    if not _publish_new(pending, dest):
                        return f"配置文件 '{new_name}' 已存在"
                finally:
                    if pending is not None:
                        pending.unlink(missing_ok=True)
                self._cache.pop(new_name, None)
            return None

        def create_default(self, config_name):
            _check_write()
            with lock:
                if config_name in self.list_available():
                    return False
                self.get_config(config_name)
                return True

        def delete(self, config_name):
            _check_write()
            with lock:
                path = self._file(config_name)
                try:
                    path.unlink()
                except OSError:
                    return False
                self._cache.pop(config_name, None)
                return True

        def drop_cache(self, config_name):
            _check_write()
            with lock:
                self._cache.pop(config_name, None)

        def clear_cache(self):
            _check_write()
            with lock:
                self._cache.clear()

    return ConfigSetManager


def install_settings(package, data_root, plugin):
    raw = pkgutil.get_data(__name__, "plugin_config_models.py")
    if (
        raw is None
        or hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest() != SCHEMA_SHA256
    ):
        raise RuntimeError("fixed GsCore configuration schema checksum mismatch")
    package("gsuid_core.utils.plugins_config")
    models = package("gsuid_core.utils.plugins_config.models")
    vars(models).update(MODELS)
    module = package("gsuid_core.utils.plugins_config.gs_config")
    registry = {}
    module.StringConfig = configuration_type(data_root, plugin, registry=registry)
    module.ConfigSetManager = configuration_manager_type(data_root, module.StringConfig)
    module.all_config_list = MappingProxyType(registry)
    module.reconcile_config = reconcile_config
