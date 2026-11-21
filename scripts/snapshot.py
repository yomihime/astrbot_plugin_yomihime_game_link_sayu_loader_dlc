"""Create reproducible archives directly from the pinned Git objects."""

import hashlib
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path, PureWindowsPath

try:
    from ntpath import isreserved
except ImportError:  # Python 3.12

    def isreserved(name):
        return PureWindowsPath(name).is_reserved()


def file_digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def repository_path(directory, spec):
    # Reuse a developer's checkout without reading or changing its working tree.
    checkout = directory / spec["directory"]
    if (checkout / ".git").exists():
        return checkout
    return directory / "upstream.git"


def git(repo, *args):
    return ["git", "--no-replace-objects", "-C", str(repo), *args]


def fetch(repository, spec):
    url = f"https://github.com/{spec['repository']}.git"
    commit = spec["commit"]
    if not repository.exists():
        subprocess.run(["git", "init", "--bare", str(repository)], check=True)
    have_commit = subprocess.run(
        git(repository, "--no-lazy-fetch", "cat-file", "-e", commit + "^{commit}"),
        stderr=subprocess.DEVNULL,
    )
    if have_commit.returncode:
        subprocess.run(
            git(repository, "fetch", "--no-tags", "--depth=1", url, commit), check=True
        )
    # Complete only the package trees if this is an existing partial clone.
    objects = subprocess.check_output(
        git(
            repository,
            "rev-list",
            "--objects",
            "--missing=print",
            "--no-walk",
            commit,
            "--",
            spec["package"],
            "LICENSE",
            "ICON.png",
        )
    )
    missing = [row[1:] for row in objects.splitlines() if row.startswith(b"?")]
    if missing:
        subprocess.run(
            git(
                repository,
                "fetch",
                "--no-tags",
                "--no-write-fetch-head",
                "--stdin",
                url,
            ),
            input=b"\n".join(missing) + b"\n",
            check=True,
        )


def zip_entry(name):
    entry = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
    entry.create_system = 3
    entry.external_attr = 0o100644 << 16
    return entry


def safe_name(name):
    parts = name.split("/")
    return all(
        part
        and part not in (".", "..")
        and not part.endswith((" ", "."))
        and not any(char in '<>:"\\|?*' or ord(char) < 32 for char in part)
        and not isreserved(part)
        for part in parts
    )


def write_snapshot(repository, spec, output):
    package = spec["package"]
    tree = subprocess.check_output(
        git(
            repository,
            "--no-lazy-fetch",
            "ls-tree",
            "-r",
            "-z",
            spec["commit"],
            "--",
            package,
            "LICENSE",
            "ICON.png",
        )
    )
    names = set()
    # Read raw blobs. Git archives and working trees can apply end-of-line or
    # attribute conversions, changing the Python sources and snapshot digest.
    with (
        subprocess.Popen(
            git(repository, "--no-lazy-fetch", "cat-file", "--batch"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        ) as process,
        zipfile.ZipFile(output, "w") as dest,
    ):
        try:
            for record in tree.split(b"\0"):
                if not record:
                    continue
                header, raw_name = record.split(b"\t", 1)
                mode, kind, oid = header.split()
                name = raw_name.decode("utf-8")
                if kind != b"blob" or mode not in (b"100644", b"100755"):
                    raise ValueError(f"Unsupported Git object: {name!r}")
                if not safe_name(name) or not (
                    name.startswith(package + "/") or name in ("LICENSE", "ICON.png")
                ):
                    raise ValueError(f"Unsafe snapshot entry: {name!r}")
                if name.casefold() in names:
                    raise ValueError(f"Duplicate snapshot entry: {name!r}")
                names.add(name.casefold())
                process.stdin.write(oid + b"\n")
                process.stdin.flush()
                result = process.stdout.readline().split()
                if len(result) != 3 or result[:2] != [oid, b"blob"]:
                    raise ValueError(f"Missing Git blob for {name!r}; run prepare.py")
                size = int(result[2])
                raw = process.stdout.read(size)
                if len(raw) != size or process.stdout.read(1) != b"\n":
                    raise RuntimeError(f"Incomplete Git blob for {name!r}")
                dest.writestr(zip_entry(name), raw)
        finally:
            process.stdin.close()
        if process.wait():
            raise RuntimeError(f"Failed to read pinned Git objects for {package}")
    if not {"license", (package + "/__init__.py").casefold()} <= names:
        raise ValueError(f"Missing package files for {package}")


def pinned_snapshot(directory, spec, runtime):
    expected = spec["snapshot_sha256"]
    target = runtime / f"{spec['package']}-{expected}.zip"
    if target.exists() and file_digest(target) == expected:
        return target
    runtime.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=runtime, suffix=".tmp", delete=False) as temp:
        pending = Path(temp.name)
    try:
        write_snapshot(repository_path(directory, spec), spec, pending)
        if file_digest(pending) != expected:
            raise ValueError(f"Source snapshot digest differs for {spec['package']}")
        os.replace(pending, target)
    finally:
        pending.unlink(missing_ok=True)
    return target
