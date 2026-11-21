import hashlib
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from snapshot import file_digest, pinned_snapshot, safe_name, write_snapshot


class SnapshotTest(unittest.TestCase):
    def test_pin_covers_source_and_assets_and_ignores_worktree(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "checkout"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)

            def git(*args):
                return subprocess.check_output(["git", "-C", str(repo), *args])

            git("config", "core.autocrlf", "false")
            (repo / "Game" / "service").mkdir(parents=True)
            sources = {
                "LICENSE": b"license\n",
                "ICON.png": b"root image",
                "Game/__init__.py": b"\n",
                "Game/service/__init__.py": b"some_handler = 1\n",
                "Game/service/asset.bin": bytes(range(256)),
            }
            for name, data in sources.items():
                (repo / name).write_bytes(data)
            (repo / "README.md").write_text("repo docs", encoding="utf-8")
            git("add", ".")
            git(
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-qm",
                "fixture",
            )
            spec = {
                "directory": repo.name,
                "package": "Game",
                "commit": git("rev-parse", "HEAD").decode().strip(),
            }

            archive = root / "snapshot.zip"
            write_snapshot(repo, spec, archive)
            with zipfile.ZipFile(archive) as bundle:
                self.assertEqual(
                    {n: bundle.read(n) for n in bundle.namelist()}, sources
                )
            spec["snapshot_sha256"] = file_digest(archive)
            git("config", "core.autocrlf", "true")
            (repo / "Game/__init__.py").write_bytes(b"changed\r\n")
            (repo / "Game/private.txt").write_bytes(b"untracked")
            runtime = root / "runtime"
            cached = pinned_snapshot(root, spec, runtime)
            self.assertEqual(archive.read_bytes(), cached.read_bytes())
            cached.write_bytes(b"corrupt")
            self.assertEqual(
                file_digest(pinned_snapshot(root, spec, runtime)),
                spec["snapshot_sha256"],
            )
            spec["snapshot_sha256"] = hashlib.sha256(b"other pin").hexdigest()
            with self.assertRaisesRegex(ValueError, "digest differs"):
                pinned_snapshot(root, spec, runtime)

    def test_portable_path_names(self):
        self.assertTrue(safe_name("Game/中文图片.png"))
        for bad in (
            "/tmp/file",
            "../other",
            "Game/../file",
            "Game/C:ads",
            "Game/NUL.txt",
            "Game/file. ",
            "Game/a\\b",
            "Game//file",
        ):
            with self.subTest(bad=bad):
                self.assertFalse(safe_name(bad))
