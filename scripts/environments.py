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


_LOCK_CHECK = """
import importlib.metadata as metadata
import json
from pathlib import Path
import sys

root, pins = json.load(sys.stdin)
root = Path(root).resolve()
if Path(sys.prefix).resolve() != root:
    raise SystemExit("selected interpreter is outside the private environment")
for name, expected in pins.items():
    dist = metadata.distribution(name)
    if (dist.version != expected or
            not Path(dist.locate_file("")).resolve().is_relative_to(root)):
        raise SystemExit(f"private package differs from lock: {name}")
"""


def verify_versions(exe, inputs):
    pins = {}
    for line in inputs.decode("utf-8").splitlines():
        name, equals, version = line.partition("==")
        if equals:
            pins[name.strip()] = version.strip()
    subprocess.run(
        [str(exe), "-IB", "-c", _LOCK_CHECK],
        input=json.dumps([str(exe.parent.parent), pins]),
        text=True,
        check=True,
    )


def verify_environment(exe, inputs):
    subprocess.run([str(exe), "-I", "-m", "pip", "--isolated", "check"], check=True)
    # These upstream imports use APIs whose version constraints in wheel
    # metadata alone do not guarantee compatibility (notably PageSchema).
    probes = {
        "fastapi-amis-admin": "from fastapi_amis_admin.amis.components import PageSchema",
        "pydantic-ai-slim": "from pydantic_ai import RunContext",
        "httpx2": "from httpx2 import AsyncClient",
        "sqlmodel": "from sqlmodel import Field, col, select",
    }
    packages = {
        row.partition("==")[0].lower().replace("_", "-")
        for row in inputs.decode().splitlines()
    }
    checks = [statement for package, statement in probes.items() if package in packages]
    if checks:
        subprocess.run([str(exe), "-IB", "-c", "\n".join(checks)], check=True)


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
    env = directory / name
    exe = python_path(env)
    private_lock = env / "requirements.txt"
    if (
        record["requirements"] != hashlib.sha256(requirements).hexdigest()
        or not exe.is_file()
        or not private_lock.is_file()
        or private_lock.read_bytes() != requirements
    ):
        raise RuntimeError(f"Run prepare.py for {directory.name}")
    verify_versions(exe, requirements)
    return exe, record


def prepare_environment(directory, requirements):
    inputs = requirements.read_bytes()
    base = [str(Path(sys._base_executable).resolve()), sys.version]
    try:
        exe, record = selected(directory, inputs)
        if record["base"] == base:
            verify_environment(exe, inputs)
            return exe
    except (RuntimeError, subprocess.CalledProcessError):
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
                "--isolated",
                "--require-virtualenv",
                "install",
                "--index-url",
                "https://pypi.org/simple",
                "--disable-pip-version-check",
                "--only-binary=:all:",
                "--no-deps",
                "--cache-dir",
                str(directory / "wheel-cache"),
                "-r",
                str(lock),
            ],
            check=True,
        )
        verify_environment(exe, inputs)
        verify_versions(exe, inputs)
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
