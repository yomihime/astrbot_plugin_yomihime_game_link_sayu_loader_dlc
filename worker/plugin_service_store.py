"""Profile-owned GsCore plugin configuration and its mutable public views."""

from collections.abc import Mapping, MutableMapping
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from threading import RLock

from port_errors import Unsupported


class RestrictedConfiguration(Unsupported, NotImplementedError):
    """The requested operation is outside the profile's configuration authority."""


class _StoreInterface:
    """Public store methods share the profile's persistence and active routing state."""

    __slots__ = (
        "get",
        "get_all",
        "set",
        "mark_dirty",
        "save",
        "save_dirty",
        "save_all",
    )

    def __init__(self, store):
        for key in self.__slots__:
            setattr(self, key, getattr(store, key))


class _View(MutableMapping):
    def __init__(self, store, path, values):
        if not isinstance(values, Mapping):
            raise TypeError("plugin and service configurations must be mappings")
        self._store = store
        self._path = path
        self._items = {key: self._field(key, value) for key, value in values.items()}

    def _field(self, key, value):
        path = self._path
        if not path:
            self._store._check_plugin(key)
        elif path == (self._store.name, "sv"):
            if not isinstance(key, str) or not key:
                raise ValueError("service name must be a non-empty string")
        elif key == "enabled":
            if type(value) is not bool:
                raise ValueError("enabled must be boolean")
            return value
        elif key == "priority":
            if type(value) is not int:
                raise ValueError("priority must be an integer")
            return value
        elif key != "sv" or len(path) != 1:
            raise RestrictedConfiguration(
                "only enabled, priority and nested service settings are writable"
            )
        return _View(self._store, (*path, key), value)

    def __getitem__(self, key):
        self._store._check_active()
        if not self._path:
            self._store._check_plugin(key)
        elif len(self._path) in (1, 3):
            allowed = (
                {"enabled", "priority", "sv"}
                if len(self._path) == 1
                else {"enabled", "priority"}
            )
            if key not in allowed:
                raise RestrictedConfiguration(
                    "requested field is outside the supported profile settings"
                )
        return self._items[key]

    def __iter__(self):
        self._store._check_active()
        return iter(tuple(self._items))

    def __len__(self):
        self._store._check_active()
        return len(self._items)

    def __setitem__(self, key, value):
        self.update({key: value})

    def setdefault(self, key, default=None):
        with self._store.lock:
            if key not in self:
                self[key] = default
            return self[key]

    def update(self, *args, **kwargs):
        with self._store.lock:
            self._store._authorize()
            batch = {
                key: self._field(key, value)
                for key, value in dict(*args, **kwargs).items()
            }
            self._items.update(batch)
            self._store._sync_targets()

    def __delitem__(self, key):
        with self._store.lock:
            self._store._authorize()
            self[key]
            del self._items[key]
            self._store._sync_targets()

    def clear(self):
        with self._store.lock:
            self._store._authorize()
            self._items.clear()
            self._store._sync_targets()

    def _snapshot(self):
        return {
            k: v._snapshot() if isinstance(v, _View) else v
            for k, v in self._items.items()
        }

    def copy(self):
        self._store._check_active()
        return dict(self)


class ProfileServiceStore:
    def __init__(self, root, name, grant):
        if (
            not isinstance(name, str)
            or not name
            or name in {".", ".."}
            or any(c in name for c in "/\\:\0")
        ):
            raise ValueError("invalid profile plugin name")
        self.name = name
        self._root = Path(root).resolve()
        self.path = self._root / "services.sqlite3"
        self._grant = grant
        self._active = True
        self.lock = RLock()
        self._targets = {}
        self._dirty = False
        self._baseline = None
        self._legacy_raw = None
        saved = self._read_database()
        self._all = _View(self, (), {} if saved is None else {name: saved})
        self._read_legacy()

    def _check_active(self):
        if not self._active:
            raise RestrictedConfiguration("retired profile configuration")

    def _check_plugin(self, name):
        self._check_active()
        if name != self.name:
            raise RestrictedConfiguration(
                "only the current plugin profile is accessible"
            )

    def _authorize(self):
        self._check_active()
        grant = self._grant.get()
        if grant is None or not grant.writable or grant.profile is not self:
            raise RestrictedConfiguration(
                "service mutation requires an active management scope"
            )

    def public_view(self):
        return _StoreInterface(self)

    def _database_path(self):
        for suffix in ("-journal", "-wal", "-shm"):
            self._private(self.path.with_name(self.path.name + suffix))
        return self._private(self.path)

    def _private(self, path):
        if (
            path.resolve() != path
            or not path.is_relative_to(self._root)
            or (path.exists() and (not path.is_file() or path.stat().st_nlink != 1))
        ):
            raise RestrictedConfiguration(
                "profile configuration requires a private regular file"
            )
        return path

    def _read_database(self):
        if not self._database_path().exists():
            return None
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
            tables = {
                r[0]
                for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "profile_service_config" in tables:
                rows = db.execute(
                    "SELECT name, payload FROM profile_service_config"
                ).fetchall()
                if rows:
                    if len(rows) != 1 or rows[0][0] != self.name:
                        raise RestrictedConfiguration(
                            "service database belongs to another plugin"
                        )
                    self._baseline = rows[0][1]
                    return json.loads(self._baseline)
            result = {}
            if "settings" in tables:
                for kind, name, enabled, priority in db.execute(
                    "SELECT kind,name,enabled,priority FROM settings"
                ):
                    if kind == "plugin":
                        self._check_plugin(name)
                        row = result
                    elif kind == "service":
                        row = result.setdefault("sv", {}).setdefault(name, {})
                    else:
                        raise RestrictedConfiguration("unknown legacy service record")
                    if enabled is not None:
                        if enabled not in (0, 1):
                            raise ValueError("invalid stored activation")
                        row["enabled"] = bool(enabled)
                    if priority is not None:
                        row["priority"] = priority
        return result or None

    def _read_legacy(self):
        core = self._private(self._root / "config.json")
        entry = self._private(self._root / "plugins_configs" / (self.name + ".json"))
        data = None
        if core.exists():
            raw = core.read_bytes()
            config = json.loads(raw)
            if "plugins" in config:
                scoped = _View(self, (), config["plugins"])
                if self.name in scoped:
                    data = scoped[self.name]
                self._legacy_raw = raw
        if entry.exists() and self._baseline is None:
            data = _View(self, (self.name,), json.loads(entry.read_bytes()))
        if self.name not in self._all and data is not None:
            self._all._items[self.name] = data
            self._dirty = True

    def _replace_file(self, path, content):
        pending = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
                pending = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(pending, self._private(path))
        finally:
            if pending is not None:
                pending.unlink(missing_ok=True)

    def _complete_legacy(self):
        if self._legacy_raw is None:
            return
        # The settings commit must be durable before retiring the old key.
        # Serialize file cleanup with other Loader generations sharing the database.
        with closing(sqlite3.connect(self._database_path())) as db, db:
            db.execute("BEGIN IMMEDIATE")
            core = self._private(self._root / "config.json")
            raw = core.read_bytes()
            config = json.loads(raw)
            if "plugins" in config:
                if raw != self._legacy_raw:
                    raise RuntimeError(
                        "legacy profile configuration changed during import"
                    )
                if config["plugins"]:
                    if self._baseline is None:
                        raise RuntimeError(
                            "legacy plugin configuration has not been persisted"
                        )
                    backup = self._private(self._root / "config_backup.json")
                    if not backup.exists():
                        self._replace_file(backup, raw)
                del config["plugins"]
                self._replace_file(
                    core,
                    json.dumps(config, ensure_ascii=False, indent=4).encode("utf8"),
                )
            self._legacy_raw = None

    def get_all(self):
        self._check_active()
        return self._all

    def get(self, plugin_name):
        self._check_plugin(plugin_name)
        return self._all.get(plugin_name, {})

    def set(self, plugin_name, data):
        with self.lock:
            self._authorize()
            self._all[plugin_name] = data
            self._dirty = True

    def mark_dirty(self, plugin_name):
        with self.lock:
            self._authorize()
            self._check_plugin(plugin_name)
            self._dirty = True

    def save(self, plugin_name):
        with self.lock:
            self._authorize()
            self._check_plugin(plugin_name)
            if plugin_name in self._all:
                payload = json.dumps(
                    self._all[plugin_name]._snapshot(),
                    ensure_ascii=False,
                )
                self._database_path().parent.mkdir(exist_ok=True, parents=True)
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute(
                        "CREATE TABLE IF NOT EXISTS profile_service_config(name TEXT PRIMARY KEY,payload TEXT NOT NULL)"
                    )
                    rows = db.execute(
                        "SELECT name,payload FROM profile_service_config"
                    ).fetchall()
                    if rows and (len(rows) != 1 or rows[0][0] != self.name):
                        raise RestrictedConfiguration(
                            "service database belongs to another plugin"
                        )
                    current = rows[0][1] if rows else None
                    if current not in (self._baseline, payload):
                        raise RuntimeError(
                            "profile configuration changed in another generation"
                        )
                    db.execute(
                        "INSERT OR REPLACE INTO profile_service_config VALUES(?,?)",
                        (self.name, payload),
                    )
                self._baseline = payload
                self._dirty = False
            self._complete_legacy()

    def save_dirty(self):
        with self.lock:
            self._authorize()
            if self._dirty or self._legacy_raw is not None:
                self.save(self.name)

    def save_all(self):
        self.save(self.name)

    def _after_load(self):
        grant = self._grant.get()
        if grant is not None and grant.writable:
            self.save_all()

    def _sync_targets(self):
        plugin = self._all._items.get(self.name, {})
        services = plugin.get("sv", {})
        for (kind, name), (target, enabled, priority) in self._targets.items():
            value = plugin if kind == "plugin" else services.get(name, {})
            target.enabled = value.get("enabled", enabled)
            target.priority = value.get("priority", priority)

    def restore(self, kind, target):
        with self.lock:
            self._check_active()
            if kind == "plugin":
                self._check_plugin(target.name)
            self._targets[kind, target.name] = target, target.enabled, target.priority
            top = self._all._items.setdefault(self.name, _View(self, (self.name,), {}))
            node = top
            if kind == "service":
                services = top._items.setdefault(
                    "sv", _View(self, (self.name, "sv"), {})
                )
                node = services._items.setdefault(
                    target.name, _View(self, (self.name, "sv", target.name), {})
                )
            for key in ("enabled", "priority"):
                node._items.setdefault(key, node._field(key, getattr(target, key)))
            self._dirty = True
            target.enabled = node["enabled"]
            target.priority = node["priority"]

    def update(self, kind, target, changes, is_lazy=True):
        with self.lock:
            self._authorize()
            if self._targets.get((kind, target.name), (None,))[0] is not target:
                raise RestrictedConfiguration(
                    "service does not belong to the active profile"
                )
            if changes.keys() - {"enabled", "priority"}:
                raise RestrictedConfiguration(
                    "only private activation and priority can be changed"
                )
            top = self._all.setdefault(self.name, {})
            node = (
                top
                if kind == "plugin"
                else top.setdefault("sv", {}).setdefault(target.name, {})
            )
            node.update(changes)
            self._dirty = True
            if not is_lazy:
                self.save(self.name)
