"""Publish complete private environments without modifying previously bound paths."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path


def python_path(env):
    return env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def selected(directory, requirements):
    pointer = directory / "environment.json"
    try:
        record = json.loads(pointer.read_bytes())
    except FileNotFoundError:
        raise RuntimeError(f"Run prepare.py for {directory.name}") from None
    name = record["environment"]
    if Path(name).name != name or not name.startswith("env-"):
        raise ValueError("Invalid private environment name")
    exe = python_path(directory / name)
    if (
        record["requirements"] != hashlib.sha256(requirements).hexdigest()
        or not exe.is_file()
    ):
        raise RuntimeError(f"Run prepare.py for {directory.name}")
    return exe, record


def prepare_environment(directory, requirements):
    inputs = requirements.read_bytes()
    base = [str(Path(sys._base_executable).resolve()), sys.version]
    try:
        exe, record = selected(directory, inputs)
        if record["base"] == base:
            return exe
    except RuntimeError:
        pass

    # Venv launchers embed absolute paths. Build at the final unique location
    # and only publish the selection once installation has succeeded.
    env = Path(tempfile.mkdtemp(dir=directory, prefix="env-"))
    exe = python_path(env)
    try:
        venv.EnvBuilder(with_pip=True).create(env)
        lock = env / "requirements.txt"
        lock.write_bytes(inputs)
        subprocess.run(
            [
                str(exe),
                "-I",
                "-m",
                "pip",
                "--require-virtualenv",
                "install",
                "-r",
                str(lock),
            ],
            check=True,
        )
        record = {
            "environment": env.name,
            "requirements": hashlib.sha256(inputs).hexdigest(),
            "base": base,
        }
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as file:
            pending = Path(file.name)
            file.write(json.dumps(record).encode())
        try:
            os.replace(pending, directory / "environment.json")
        finally:
            pending.unlink(missing_ok=True)
    except Exception:
        shutil.rmtree(env)
        raise
    return exe
