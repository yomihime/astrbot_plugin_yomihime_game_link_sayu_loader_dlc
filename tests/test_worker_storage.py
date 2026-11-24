"""Exercise the process-wide storage guard in a disposable child process."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


def exercise(root):
    import sqlite3

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
    from worker_storage import install_write_guard

    generation = root / "generation"
    profile = root / "profile"
    for path in (generation, profile):
        path.mkdir()
    installed = generation / "worker.pyz"
    external = root / "original-dependency"
    installed.write_bytes(b"worker")
    external.write_bytes(b"old")
    install_write_guard(generation, profile)
    for action in (
        lambda: external.write_bytes(b"new"),
        lambda: installed.write_bytes(b"new"),
        lambda: sqlite3.connect((root / "shared.db").as_uri(), uri=True),
    ):
        try:
            action()
        except PermissionError:
            continue
        raise AssertionError("mutation outside private storage succeeded")
    assert external.read_bytes() == b"old" and installed.read_bytes() == b"worker"
    assert not (root / "shared.db").exists()
    db = profile / "private.db"
    connection = sqlite3.connect(db.as_uri(), uri=True)
    try:
        connection.execute("CREATE TABLE observations (value TEXT)")
        connection.execute("INSERT INTO observations VALUES ('persisted')")
        connection.commit()
    finally:
        connection.close()
    connection = sqlite3.connect(db)
    try:
        assert connection.execute("SELECT value FROM observations").fetchone() == (
            "persisted",
        )
    finally:
        connection.close()
    scratch = generation / "copy"
    scratch.mkdir()
    scratch.rmdir()


class StorageTest(unittest.TestCase):
    def test_generation_and_private_database(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(
                [sys.executable, "-IB", __file__, directory], check=True, cwd=directory
            )


if __name__ == "__main__":
    exercise(Path(sys.argv[1]))
