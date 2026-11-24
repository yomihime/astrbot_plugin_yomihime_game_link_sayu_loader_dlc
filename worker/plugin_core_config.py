"""Public framework configuration scoped to a single worker profile."""

from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

from port_errors import Unsupported
from service_settings import _grant

# Non-secret defaults in gsuid_core/config.py at 96cd7353. The worker
# never discovers a gateway, copies host secrets, or provisions operator IDs.
DEFAULTS = {
    "HOST": "localhost",
    "PORT": "8765",
    "LANGUAGE": "zh-cn",
    "masters": [],
    "superusers": [],
    "framework_aliases": ["GsCore", "gsuid_core"],
    "misfire_grace_time": 90,
    "enable_empty_start": True,
    "command_start": [],
    "log": {"level": "INFO", "output": ["stdout", "stderr", "file"], "module": False},
    "sv": {},
    "ENABLE_HTTP": False,
}
WRITABLE = {"LANGUAGE", "log"}
PROTECTED = {
    "REGISTER_CODE",
    "WS_TOKEN",
    "TRUSTED_IPS",
    "web_max_sessions",
    "web_cors_origins",
}


class ProfileConfig:
    def __init__(self, root, plugin_store=None):
        self._plugin_store = plugin_store
        self.path = Path(root).resolve() / "framework-config.sqlite3"
        self.values = deepcopy(DEFAULTS)
        self.pending = {}
        self.update_config()

    @property
    def config(self):
        return deepcopy(self.values)

    def _check_path(self):
        if self.path.resolve() != self.path or (
            self.path.exists() and self.path.stat().st_nlink != 1
        ):
            raise Unsupported("framework configuration is outside private storage")

    def update_config(self):
        self._check_path()
        values = deepcopy(DEFAULTS)
        if self.path.exists():
            with closing(
                sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
            ) as db:
                for key, raw in db.execute("SELECT name,value FROM settings"):
                    if key in WRITABLE:
                        values[key] = json.loads(raw)
        self.pending.clear()
        self.values = values

    def get_config(self, key):
        if key in PROTECTED:
            raise Unsupported("host configuration is unavailable")
        return deepcopy(self.values.get(key, {}))

    @staticmethod
    def _can_update(key):
        if key not in DEFAULTS and key not in PROTECTED:
            return False
        grant = _grant.get()
        if key not in WRITABLE or not grant or not grant.writable:
            raise Unsupported("framework setting requires a private management grant")
        return True

    def lazy_set_config(self, key, value):
        if self._can_update(key):
            if key == "LANGUAGE":
                if not isinstance(value, str) or len(value) > 64:
                    raise ValueError("invalid profile language")
            elif key == "log":
                if (
                    not isinstance(value, dict)
                    or set(value) - {"level", "output", "module"}
                    or value.get("level", "INFO")
                    not in {
                        "TRACE",
                        "DEBUG",
                        "INFO",
                        "SUCCESS",
                        "WARNING",
                        "ERROR",
                        "CRITICAL",
                    }
                    or not isinstance(value.get("output", []), list)
                    or any(
                        sink not in ("stdout", "stderr", "file")
                        for sink in value.get("output", [])
                    )
                    or type(value.get("module", False)) is not bool
                ):
                    raise ValueError("invalid profile log settings")
            self.values[key] = deepcopy(value)
            self.pending[key] = deepcopy(value)

    def write_config(self):
        if not self.pending:
            return
        for key in self.pending:
            self._can_update(key)
        self._check_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS settings(name TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            db.executemany(
                "INSERT INTO settings(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                [(k, json.dumps(v, allow_nan=False)) for k, v in self.pending.items()],
            )
        self.pending.clear()

    def lazy_write_config(self):
        self.write_config()
        if self._plugin_store is not None:
            self._plugin_store.save_dirty()

    def set_config(self, key, value):
        if not self._can_update(key):
            return False
        previous_value = self.values[key]
        previous_pending = dict(self.pending)
        self.lazy_set_config(key, value)
        try:
            self.write_config()
        except Exception:
            self.values[key] = previous_value
            self.pending = previous_pending
            raise
        return True


def install_core_config(module, data_root, plugin_store=None):
    module.CONFIG_DEFAULT = deepcopy(DEFAULTS)
    module.core_config = ProfileConfig(data_root, plugin_store)
    if plugin_store is not None:
        module.plugin_config_store = plugin_store.public_view()
    return module.core_config
