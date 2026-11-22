"""Private persisted activation and dispatch priority for one plugin profile."""

from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import sqlite3
import threading


@dataclass
class _ManagementGrant:
    writable: bool


_grant = ContextVar("sayu_service_management", default=None)


@contextmanager
def management_scope(*, writable=False):
    previous = _grant.get()
    if writable and previous is not None and not previous.writable:
        raise NotImplementedError("read-only service management scope")
    grant = _ManagementGrant(writable)
    token = _grant.set(grant)
    try:
        yield
    finally:
        grant.writable = False
        _grant.reset(token)


class ServiceSettings:
    def __init__(self, root):
        self.path = root / "services.sqlite3"
        self.lock = threading.RLock()

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.resolve() != self.path:
            raise ValueError("service settings path outside private storage")
        db = sqlite3.connect(self.path)
        db.execute(
            "CREATE TABLE IF NOT EXISTS settings (kind TEXT, name TEXT, enabled INTEGER, "
            "priority INTEGER, PRIMARY KEY(kind, name))"
        )
        return db

    def restore(self, kind, target):
        if not self.path.exists():
            return
        with self.lock, closing(self.connect()) as db:
            row = db.execute(
                "SELECT enabled, priority FROM settings WHERE kind=? AND name=?",
                (kind, target.name),
            ).fetchone()
            if row:
                enabled, priority = row
                if enabled is not None:
                    if enabled not in (0, 1):
                        raise ValueError("invalid service activation")
                    target.enabled = bool(enabled)
                if priority is not None:
                    if type(priority) is not int:
                        raise ValueError("invalid service priority")
                    target.priority = priority

    def update(self, kind, target, changes):
        grant = _grant.get()
        if grant is None or not grant.writable:
            raise NotImplementedError("service mutation requires a management scope")
        if changes.keys() - {"enabled", "priority"}:
            raise NotImplementedError(
                "only private activation and priority can be changed"
            )
        if "enabled" in changes and type(changes["enabled"]) is not bool:
            raise ValueError("enabled must be boolean")
        if "priority" in changes and type(changes["priority"]) is not int:
            raise ValueError("priority must be an integer")
        if not changes:
            return
        with self.lock, closing(self.connect()) as db:
            with db:
                db.execute(
                    "INSERT INTO settings(kind,name,enabled,priority) VALUES(?,?,?,?) "
                    "ON CONFLICT(kind,name) DO UPDATE SET "
                    "enabled=coalesce(excluded.enabled,enabled), priority=coalesce(excluded.priority,priority)",
                    (
                        kind,
                        target.name,
                        changes.get("enabled"),
                        changes.get("priority"),
                    ),
                )
            for key, value in changes.items():
                setattr(target, key, value)
