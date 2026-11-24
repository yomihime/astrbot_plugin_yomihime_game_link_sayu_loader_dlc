"""Keep Python filesystem mutations in the worker generation and private profile."""

import os
from pathlib import Path
import socket
import sys
from urllib.parse import urlsplit
from urllib.request import url2pathname


def install_offline_guard():
    """Offline comparisons still execute upstream code and surface network errors."""
    from port_errors import OfflineNetwork

    fallback = getattr(getattr(socket, "_fallback_socketpair", None), "__code__", None)

    def check(event, args):
        if event in ("socket.bind", "socket.connect") and fallback is not None:
            if sys._getframe(1).f_code is fallback and args[1][0] in (
                "127.0.0.1",
                "::1",
            ):
                return  # CPython's internal event-loop wakeup pair.
        if event in {
            "socket.connect",
            "socket.getaddrinfo",
            "socket.gethostbyname",
            "socket.gethostbyaddr",
            "socket.bind",
            "socket.sendto",
            "socket.sendmsg",
        }:
            raise OfflineNetwork("network disabled for this test profile")

    sys.addaudithook(check)


def install_write_guard(generation, profile):
    roots = (Path(generation).resolve(), Path(profile).resolve())
    mutations = {
        "os.mkdir",
        "os.rmdir",
        "os.remove",
        "os.truncate",
        "os.chmod",
        "os.chown",
        "os.utime",
        "os.setxattr",
        "os.removexattr",
    }

    def check_path(value):
        if not isinstance(value, (str, bytes)):
            raise PermissionError("expected a filename for a worker mutation")
        path = Path(os.fsdecode(value))
        if path.drive.startswith("\\\\"):
            raise PermissionError("worker storage must be local")
        path = path.resolve()
        if path == roots[0] / "worker.pyz":
            raise PermissionError("worker executable is immutable")
        if not any(path.is_relative_to(root) for root in roots):
            raise PermissionError("write outside private worker storage")
        if path.is_file() and path.stat().st_nlink > 1:
            raise PermissionError("worker storage must use private files")

    def check(event, args):
        if event == "open":
            path, _, flags = args
            if isinstance(path, int):
                return  # fdopen wraps a descriptor already opened by this process.
            if flags & (
                os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
            ):
                check_path(path)
        elif event == "sqlite3.connect":
            database = os.fsdecode(args[0])
            if database == ":memory:":
                return
            if database.startswith("file:"):
                uri = urlsplit(database)
                if uri.netloc or uri.fragment:
                    raise PermissionError("non-local SQLite URI")
                database = url2pathname(uri.path)
            check_path(database)
        elif event in mutations:
            check_path(args[0])
            if event in {
                "os.remove",
                "os.rmdir",
                "os.mkdir",
                "os.chmod",
                "os.chown",
                "os.utime",
            } and args[-1] not in (None, -1):
                raise PermissionError("directory-relative mutation is unavailable")
        elif event == "os.rename":
            if any(x not in (-1, None) for x in args[2:]):
                raise PermissionError("directory-relative rename is unavailable")
            check_path(args[0])
            check_path(args[1])
        elif event in {
            "os.link",
            "os.symlink",
            "subprocess.Popen",
            "os.system",
            "os.startfile",
            "os.exec",
            "os.spawn",
            "os.posix_spawn",
            "os.fork",
        }:
            raise PermissionError("operation requires a separate host capability")

    sys.addaudithook(check)
