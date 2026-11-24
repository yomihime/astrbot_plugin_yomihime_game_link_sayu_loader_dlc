import importlib
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
environments = importlib.import_module("environments")


class PrivatePreparationTest(unittest.TestCase):
    def test_reused_environment_checks_installed_distribution(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = root / "env-fixture"
            environments.venv.EnvBuilder(with_pip=False).create(env)
            exe = environments.python_path(env)
            purelib = subprocess.check_output(
                [
                    str(exe),
                    "-IB",
                    "-c",
                    "import sysconfig;print(sysconfig.get_path('purelib'))",
                ],
                text=True,
            ).strip()
            metadata = Path(purelib) / "fixture-1.dist-info" / "METADATA"
            metadata.parent.mkdir()
            metadata.write_text("Name: fixture\nVersion: 1\n", encoding="ascii")
            lock = b"fixture==1\n"
            (env / "requirements.txt").write_bytes(lock)
            (root / "environment.json").write_text(
                json.dumps(
                    {
                        "environment": env.name,
                        "requirements": hashlib.sha256(lock).hexdigest(),
                    }
                ),
                encoding="ascii",
            )
            self.assertEqual(environments.selected(root, lock)[0], exe)
            metadata.write_text("Name: fixture\nVersion: 2\n", encoding="ascii")
            with self.assertRaises(subprocess.CalledProcessError):
                environments.selected(root, lock)

    def test_unhealthy_install_keeps_previous_selection(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pointer = root / "environment.json"
            previous = {
                "environment": "env-previous",
                "requirements": "old",
                "base": [],
            }
            pointer.write_text(json.dumps(previous), encoding="utf-8")
            old = root / previous["environment"]
            old.mkdir()
            lock = root / "lock.txt"
            lock.write_text("example==1\n", encoding="utf-8")
            error = subprocess.CalledProcessError(1, ["pip", "check"])
            with (
                patch.object(environments.venv, "EnvBuilder"),
                patch.object(
                    environments.subprocess, "run", side_effect=[None, error]
                ) as run,
                self.assertRaises(subprocess.CalledProcessError),
            ):
                environments.prepare_environment(root, lock)
            self.assertIn("--no-deps", run.call_args_list[0].args[0])
            self.assertIn("https://pypi.org/simple", run.call_args_list[0].args[0])
            self.assertEqual(run.call_args_list[1].args[0][-1], "check")
            self.assertEqual(json.loads(pointer.read_bytes()), previous)
            self.assertEqual(list(root.glob("env-*")), [old])

    def test_audit_records_readonly_attempts_and_allows_memory_sqlite(self):
        program = """
import os, sqlite3, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from audit_package_init import guard_files
source = Path.cwd() / "source"
data = Path.cwd() / "data"
source.mkdir()
data.mkdir()
outside_fd = os.open(Path.cwd() / "outside", os.O_CREAT | os.O_RDWR)
attempts = guard_files(source, (data,))
source.mkdir(exist_ok=True)
assert not attempts
sqlite3.connect(":memory:").close()
try:
    (source / "asset").write_bytes(b"unexpected")
except PermissionError:
    assert attempts == {("open", "asset"): 1}
else:
    raise AssertionError("source write allowed")
(data / "asset").write_bytes(b"private")
with os.fdopen(os.open(data / "asset", os.O_RDWR), "wb") as allowed:
    allowed.write(b"owned")
try:
    os.fdopen(outside_fd, "wb", closefd=False)
except PermissionError:
    pass
else:
    raise AssertionError("outside descriptor allowed")
os.close(outside_fd)
"""
        with tempfile.TemporaryDirectory() as temp:
            subprocess.run(
                [sys.executable, "-IB", "-c", program, str(SCRIPTS)],
                cwd=temp,
                check=True,
            )
