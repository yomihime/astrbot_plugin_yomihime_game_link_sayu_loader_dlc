"""Private persisted activation and dispatch priority for one plugin profile."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from plugin_service_store import ProfileServiceStore


@dataclass
class _ManagementGrant:
    writable: bool
    profile: object


_grant = ContextVar("sayu_service_management", default=None)
_active_profile = None


@contextmanager
def management_scope(*, writable=False):
    previous = _grant.get()
    if writable and previous is not None and not previous.writable:
        raise NotImplementedError("read-only service management scope")
    profile = previous.profile if previous is not None else _active_profile
    grant = _ManagementGrant(writable, profile)
    token = _grant.set(grant)
    try:
        yield
    finally:
        grant.writable = False
        _grant.reset(token)


class ServiceSettings(ProfileServiceStore):
    def __init__(self, root, plugin=None):
        super().__init__(root, root.name if plugin is None else plugin, _grant)


def select_service_profile(root, plugin, enabled):
    global _active_profile
    if _active_profile is not None:
        _active_profile._active = False
    _active_profile = None
    if enabled:
        _active_profile = ServiceSettings(root, plugin)
    return _active_profile
