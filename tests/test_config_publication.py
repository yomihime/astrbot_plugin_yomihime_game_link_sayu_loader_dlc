"""Run the configured publisher with the real process-wide worker write guard."""

from concurrent.futures import ThreadPoolExecutor
import ctypes
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

WORKER = Path(__file__).resolve().parents[1] / "worker"


def exercise(directory):
    sys.path.insert(0, str(WORKER))
    import worker_storage as storage

    generation, profile = directory / "generation", directory / "profile"
    generation.mkdir()
    profile.mkdir()
    worker = generation / "worker.pyz"
    worker.write_bytes(b"read only")
    outside = directory / "outside"
    outside.write_bytes(b"private")
    storage.install_write_guard(generation, profile)

    def rejected(action):
        try:
            action()
        except PermissionError:
            return
        raise AssertionError("write guard accepted an unauthorized operation")

    source = profile / "source"
    source.write_bytes(b"unchanged")
    rejected(lambda: os.link(source, profile / "alias"))
    rejected(lambda: os.link(outside, profile / "foreign-alias"))
    rejected(lambda: storage.publish_new_config(outside, directory / "new"))
    rejected(lambda: storage.publish_new_config(worker, generation / "mutated"))
    try:
        storage.publish_new_config(source, directory / "escaped")
    except ValueError:
        pass
    else:
        raise AssertionError("cross-directory publication succeeded")
    destination = profile / "competing.json"
    files = [profile / str(n) for n in range(24)]
    for n, path in enumerate(files):
        path.write_bytes(bytes([n]) * 32768)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda p: storage.publish_new_config(p, destination), files)
        )
    assert results.count(True) == 1 and results.count(False) == 23
    winner = results.index(True)
    assert destination.read_bytes() == bytes([winner]) * 32768
    assert not files[winner].exists()
    assert all(p.exists() == (i != winner) for i, p in enumerate(files))
    assert source.read_bytes() == b"unchanged" and outside.read_bytes() == b"private"

    from plugin_config_models import GsStrConfig
    from plugin_settings import configuration_manager_type, configuration_type
    from service_settings import management_scope

    settings = configuration_type(profile, "probe")
    manager = configuration_manager_type(profile, settings)
    defaults = {"field": GsStrConfig("field", "", "original")}
    configs = manager(profile / "sets", defaults)
    with management_scope(writable=True):
        assert configs.create_default("first")
        assert configs.copy("first", "copy") is None
        duplicate = configs.get_config("copy")
        duplicate.set_config("field", "kept")
        assert configs.copy("first", "copy") == "配置文件 'copy' 已存在"
        old = duplicate.CONFIG_PATH
        new = profile / "new.json"
        moved = settings("migration", [old, new], defaults)
        assert moved["field"].data == "kept" and not old.exists()

    # Exercise the actual POSIX dispatch code on any interpreter. Replace only the
    # libc entry point, leaving sys.audit and the installed storage guard active.
    for platform, symbol, args in [
        (
            "linux",
            "renameat2",
            (-100, os.fsencode(source), -100, os.fsencode(destination), 1),
        ),
        ("darwin", "renamex_np", (os.fsencode(source), os.fsencode(destination), 4)),
    ]:
        library = Mock(spec=[symbol])
        exclusive = getattr(library, symbol)
        exclusive.return_value = -1
        with (
            patch.object(sys, "platform", platform),
            patch.object(ctypes, "CDLL", return_value=library),
        ):
            with patch.object(ctypes, "get_errno", return_value=errno.EEXIST):
                assert not storage._publish_posix(source, destination)
            exclusive.assert_called_once_with(*args)
            assert len(exclusive.argtypes) == len(args)
            rejected(lambda: storage._publish_posix(outside, directory / "new"))
            exclusive.assert_called_once()
            with patch.object(ctypes, "get_errno", return_value=errno.ENOTSUP):
                try:
                    storage._publish_posix(source, destination)
                except OSError as exc:
                    assert exc.errno == errno.ENOTSUP
                else:
                    raise AssertionError("unsupported filesystem silently overwritten")
        with (
            patch.object(sys, "platform", platform),
            patch.object(ctypes, "CDLL", return_value=object()),
        ):
            try:
                storage._publish_posix(source, destination)
            except OSError as exc:
                assert exc.errno == errno.ENOSYS
            else:
                raise AssertionError("missing native symbol silently accepted")
    assert destination.read_bytes() == bytes([winner]) * 32768
    print(
        json.dumps(
            {
                "native_platform": sys.platform,
                "python": sys.version.split()[0],
                "winners": 1,
                "collisions": 23,
                "guard": "active",
                "public_config_copy_and_migration": True,
                "simulated_abi": ["linux", "darwin"],
            }
        )
    )


class PublicationTests(unittest.TestCase):
    def test_guarded_exclusive_publication(self):
        with tempfile.TemporaryDirectory() as work:
            subprocess.run(
                [sys.executable, "-IB", __file__, work], check=True, cwd=work
            )


if __name__ == "__main__":
    if len(sys.argv) == 2:
        exercise(Path(sys.argv[1]))
    else:
        unittest.main()
