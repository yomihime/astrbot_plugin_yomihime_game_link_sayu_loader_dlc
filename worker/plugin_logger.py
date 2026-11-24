"""Bounded profile diagnostics with the native logger call surface."""

from collections import Counter, deque
from hashlib import sha256
from pathlib import Path
import sys
from threading import Lock


LEVELS = {
    "TRACE": 5,
    "DEBUG": 10,
    "INFO": 20,
    "SUCCESS": 25,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}


class ProfileDiagnostics:
    def __init__(self, source_root, level):
        self.root = Path(source_root).resolve() if source_root else None
        self.configured_level = level.upper()
        self.recent = deque(maxlen=64)
        self.counts = Counter()
        self.lock = Lock()

    def record(self, level, caller):
        # Native history collects TRACE and above regardless of console thresholds.
        site = None
        if self.root is not None:
            filename = Path(caller.f_code.co_filename)
            if (
                filename.is_absolute()
                and filename.suffix == ".py"
                and filename.is_relative_to(self.root)
            ):
                site = (filename.relative_to(self.root).as_posix(), caller.f_lineno)
        # Arbitrary user text, exception messages, formatting arguments, bound
        # values and authentication headers never enter the diagnostics.
        with self.lock:
            self.recent.append((level, site))
            self.counts[level] = min(2**63 - 1, self.counts[level] + 1)


class ProfileLogger:
    def __init__(self, diagnostics, keys=()):
        self._diagnostics = diagnostics
        self._keys = frozenset(keys)

    @staticmethod
    def _key(key):
        return sha256(key.encode("utf-8")).digest()

    def bind(self, **fields):
        return ProfileLogger(
            self._diagnostics, self._keys.union(map(self._key, fields))
        )

    def new(self, **fields):
        self._keys = frozenset()
        return self.bind(**fields)

    def unbind(self, *keys):
        remaining = set(self._keys)
        for key in keys:
            digest = self._key(key)
            if digest not in remaining:
                raise KeyError(key)
            remaining.remove(digest)
        return ProfileLogger(self._diagnostics, remaining)


def _log_method(level):
    def log(self, event=None, *args, **kwargs):
        self._diagnostics.record(level, sys._getframe(1))

    return log


for _severity in LEVELS:
    setattr(ProfileLogger, _severity.lower(), _log_method(_severity))

ProfileLogger.exception = _log_method("ERROR")
ProfileLogger.warn = ProfileLogger.warning


def install_logger(module, core_config, *, source_root=None):
    settings = core_config.get_config("log")
    diagnostics = ProfileDiagnostics(source_root, settings.get("level", "INFO"))
    module.logger = ProfileLogger(diagnostics)
    return diagnostics
