"""Prepare a complete pinned upstream package and its project-private interpreter."""

import argparse
import json
import os
import subprocess
import venv
from pathlib import Path

from snapshot import fetch, pinned_snapshot, repository_path


ROOT = Path(__file__).resolve().parents[1]


def main():
    locks = json.loads((ROOT / "profiles/lock.json").read_bytes())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=locks)
    key = parser.parse_args().profile
    spec = locks[key]
    directory = ROOT / ".sayu-local" / key
    directory.mkdir(parents=True, exist_ok=True)
    fetch(repository_path(directory, spec), spec)
    pinned_snapshot(directory, spec, directory.parent / "runtime")
    env = directory / "venv"
    if not env.exists():
        venv.EnvBuilder(with_pip=True).create(env)
    exe = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(
        [
            str(exe),
            "-I",
            "-m",
            "pip",
            "--require-virtualenv",
            "install",
            "-r",
            str(ROOT / "profiles" / f"{key}-requirements.txt"),
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
