import asyncio
import ctypes
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT / "tests"))
import worker_architecture as architecture  # noqa: E402
from test_module_worker_recovery import adapter  # noqa: E402


class WindowsArchitectureTests(unittest.TestCase):
    def test_native_and_emulated_environments(self):
        cases = [
            (0, 0x014C, "x86", None),
            (0, 0x8664, "AMD64", None),
            (0, 0xAA64, "ARM64", None),
            (0x014C, 0x8664, "x86", "AMD64"),
            (0x014C, 0xAA64, "x86", "ARM64"),
            (0x8664, 0xAA64, "AMD64", "ARM64"),
            (0x01C4, 0xAA64, "ARM", "ARM64"),
        ]
        for process, native, expected_process, expected_native in cases:
            with self.subTest(process=process, native=native):
                env = {"PROCESSOR_ARCHITEW6432": "stale"}
                with (
                    patch.object(
                        architecture, "os", SimpleNamespace(name="nt", environ=env)
                    ),
                    patch.object(
                        architecture,
                        "_windows_machines",
                        return_value=(process, native),
                    ),
                ):
                    architecture.configure_worker_architecture()
                self.assertEqual(env["PROCESSOR_ARCHITECTURE"], expected_process)
                self.assertEqual(env.get("PROCESSOR_ARCHITEW6432"), expected_native)

    def test_unknown_architecture_does_not_publish_partial_environment(self):
        for pair in ((0, 0), (0xFFFF, 0x8664), (0x014C, 0xFFFF)):
            env = {}
            with (
                self.subTest(pair=pair),
                patch.object(
                    architecture, "os", SimpleNamespace(name="nt", environ=env)
                ),
                patch.object(architecture, "_windows_machines", return_value=pair),
                self.assertRaises(architecture.UnsupportedWindowsArchitecture),
            ):
                architecture.configure_worker_architecture()
            self.assertEqual(env, {})

    def test_nonwindows_never_queries_windows(self):
        query = Mock(side_effect=AssertionError("unexpected Windows query"))
        with (
            patch.object(architecture, "os", SimpleNamespace(name="posix", environ={})),
            patch.object(architecture, "_windows_machines", query),
        ):
            architecture.configure_worker_architecture()

    @unittest.skipUnless(os.name == "nt", "Windows system information ABI")
    def test_legacy_api_and_failed_modern_query(self):
        legacy = SimpleNamespace(
            GetSystemInfo=lambda ptr: setattr(ptr._obj, "architecture", 0),
            GetNativeSystemInfo=lambda ptr: setattr(ptr._obj, "architecture", 9),
        )
        with patch.object(ctypes, "WinDLL", return_value=legacy):
            self.assertEqual(architecture._windows_machines(), (0x014C, 0x8664))
        modern = SimpleNamespace(IsWow64Process2=Mock(return_value=0))
        with (
            patch.object(ctypes, "WinDLL", return_value=modern),
            patch.object(ctypes, "get_last_error", return_value=5),
            self.assertRaises(OSError) as failed,
        ):
            architecture._windows_machines()
        self.assertEqual(failed.exception.winerror, 5)


@unittest.skipUnless(os.name == "nt", "Windows worker subprocess")
class WorkerSpawnArchitectureTests(unittest.IsolatedAsyncioTestCase):
    async def test_scrubbed_subprocess_reports_its_own_architecture(self):
        probe = """import json, os, platform
from worker_architecture import configure_worker_architecture
platform.machine()  # Exercise a cache primed before worker startup.
configure_worker_architecture()
print(json.dumps([platform.machine(), os.environ["PROCESSOR_ARCHITECTURE"],
                  os.environ.get("PROCESSOR_ARCHITEW6432"),
                  "SAYU_TEST_PRIVATE" in os.environ, "HTTPS_PROXY" in os.environ]))
"""
        process, native = architecture._machine_names(*architecture._windows_machines())
        with TemporaryDirectory() as folder:
            root = Path(folder)
            with ZipFile(root / "worker.pyz", "w") as archive:
                archive.write(
                    ROOT / "worker/worker_architecture.py", "worker_architecture.py"
                )
                archive.writestr("__main__.py", probe)
            poison = {
                "PROCESSOR_ARCHITECTURE": "invented",
                "PROCESSOR_ARCHITEW6432": "invented",
                "SAYU_TEST_PRIVATE": "synthetic",
                "HTTPS_PROXY": "synthetic",
            }
            with patch.dict(os.environ, poison):
                child = await adapter(()).spawn(sys.executable, root, root / "data")
            stdout, _ = await asyncio.wait_for(child.communicate(), timeout=20)
        self.assertEqual(child.returncode, 0)
        self.assertEqual(
            json.loads(stdout),
            [native, process, None if process == native else native, False, False],
        )
