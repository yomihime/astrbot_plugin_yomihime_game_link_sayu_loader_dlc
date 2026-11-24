"""Build a localhost-only YGL candidate and byte-pinned private workers."""

import argparse
import hashlib
import io
import json
import os
import pprint
import tempfile
import zipfile
from pathlib import Path

from environments import selected
from image_assets import verified_assets
from help_assets import verified_help
from locale_assets import verified_locales
from orm_assets import verified_orm_models
from snapshot import file_digest, pinned_snapshot, zip_entry
from sign_assets import verified_sign
from scheduler_assets import verified_scheduler


ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".sayu-local"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def archive(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as out:
        for name, data in sorted(files.items()):
            info = zip_entry(name)
            out.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
    return stream.getvalue()


def worker_archive(lock, *, offline=False, public_resources_only=False, snapshot=None):
    if public_resources_only and (offline or snapshot is None):
        raise ValueError(
            "public resource mode needs a pinned snapshot and cannot be offline"
        )
    worker = ROOT / "worker"
    # Historical offline declaration probes are not part of the package runtime.
    probe_modules = {
        "declarations",
        "dependency_data",
        "discovery",
        "public_resources",
        "resource_index",
        "source_assets",
    }
    files = {
        path.relative_to(worker).as_posix(): path.read_bytes()
        for path in worker.rglob("*.py")
        if path.stem not in probe_modules
    }
    license_path = "licenses/gsuid_core-COPYING.txt"
    files.update(verified_assets(LOCAL / "reference"))
    files.update(verified_help(LOCAL / "reference"))
    files.update(verified_locales(LOCAL / "reference"))
    files.update(verified_orm_models(LOCAL / "reference"))
    files.update(verified_sign(LOCAL / "reference"))
    files.update(verified_scheduler(LOCAL / "reference"))
    files[license_path] = (worker / license_path).read_bytes()
    files["__main__.py"] = files.pop("entry.py")
    resources = json.loads((ROOT / "profiles/public_resources.json").read_bytes())
    policy = resources.get(lock["package"], {})
    manifest = json.loads(
        (ROOT / "packages/sayu_loader/yomihime.manifest.json").read_bytes()
    )
    owner_writes = tuple(
        cap["capability_id"]
        for module in manifest["modules"]
        if module["module_id"] == lock.get("module_id")
        for cap in module["capabilities"]
        if cap["effect"] == "write" and cap["privacy_floor"] == "owner"
    )
    static_urls, directories = (), ()
    if public_resources_only:
        from resource_policy import from_snapshot

        static_urls, directories = from_snapshot(snapshot, lock)
    files["game_profile.py"] = (
        f"PACKAGE = {lock['package']!r}\nPREFIX = {lock['prefix']!r}\n"
        f"RUNTIME_MODE = {lock.get('runtime_mode', 'package')!r}\n"
        f"OFFLINE = {offline!r}\n"
        f"OWNER_WRITES = {owner_writes!r}\n"
        f"PUBLIC_RESOURCES_ONLY = {public_resources_only!r}\n"
        f"STATIC_RESOURCE_URLS = {tuple(static_urls)!r}\n"
        f"STATIC_RESOURCE_DIRECTORIES = {tuple(directories)!r}\n"
        f"PUBLIC_IMAGE_ORIGINS = {tuple(policy.get('image_origins', ()))!r}\n"
        f"PUBLIC_BULK_MIRRORS = {tuple(policy.get('bulk_mirrors', ()))!r}\n"
    ).encode("utf-8")
    return archive(files)


def prepare(key, lock, *, data_root=None, offline=False, public_resources_only=False):
    directory = LOCAL / key
    requirements = ROOT / "profiles" / f"{key}-requirements.txt"
    exe, _ = selected(directory, requirements.read_bytes())
    runtime = LOCAL / "runtime"
    snapshot = pinned_snapshot(directory, lock, runtime)
    blob = worker_archive(
        lock,
        offline=offline,
        public_resources_only=public_resources_only,
        snapshot=snapshot,
    )
    sha = digest(blob)
    target = runtime / f"{key}-{sha}.pyz"
    publish_archive(target, blob)
    return (
        str(exe),
        str(target),
        sha,
        str(snapshot),
        lock["snapshot_sha256"],
        str(directory / "data" if data_root is None else data_root / key),
    )


def publish_archive(target, data):
    target.parent.mkdir(exist_ok=True, parents=True)
    if target.exists() and file_digest(target) == digest(data):
        return
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temp:
        pending = Path(temp.name)
    try:
        pending.write_bytes(data)
        os.replace(pending, target)
    finally:
        pending.unlink(missing_ok=True)


def build(*, fresh_data=False, offline=False, public_resources_only=False):
    locks = json.loads((ROOT / "profiles/lock.json").read_bytes())
    data_root = (
        Path(tempfile.mkdtemp(prefix="acceptance-", dir=LOCAL)) if fresh_data else None
    )
    workers = {
        key: prepare(
            key,
            spec,
            data_root=data_root,
            offline=offline,
            public_resources_only=public_resources_only,
        )
        for key, spec in locks.items()
    }
    files = {
        name: (ROOT / name).read_bytes()
        for name in (
            "LICENSE",
            "__init__.py",
            "main.py",
            "metadata.yaml",
            "ygl-plugin.json",
        )
    }
    package = ROOT / "packages/sayu_loader"
    for file in package.rglob("*"):
        if file.suffix in {".json", ".py"}:
            files[file.relative_to(ROOT).as_posix()] = file.read_bytes()
    files["packages/sayu_loader/sayu/runtime.py"] = (
        "WORKERS = " + pprint.pformat(workers, width=100) + "\n"
    ).encode()
    rows = [
        {"path": name, "size": len(data), "sha256": digest(data)}
        for name, data in sorted(files.items())
    ]
    inventory = b"".join(
        f"{row['path']}\0{row['size']}\0{row['sha256']}\n".encode() for row in rows
    )
    manifest = json.loads(files["packages/sayu_loader/yomihime.manifest.json"])
    files["release.json"] = json.dumps(
        {
            "schema": "ygl.release/2",
            "plugin_id": {"author": manifest["author"], "name": ROOT_NAME},
            "package_id": manifest["package_id"],
            "release": manifest["release"],
            "payload_digest": digest(inventory),
            "files": rows,
        },
        indent=2,
    ).encode()
    target = ROOT / "dist" / (ROOT_NAME + ".zip")
    target.parent.mkdir(exist_ok=True)
    publish_archive(target, archive(files))
    print(target)
    return workers


ROOT_NAME = "astrbot_plugin_yomihime_game_link_sayu_loader_dlc"
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fresh-data", action="store_true", help="bind new empty private profiles"
    )
    network = parser.add_mutually_exclusive_group()
    network.add_argument(
        "--offline",
        action="store_true",
        help="run upstream code with external networking disabled",
    )
    network.add_argument(
        "--public-resources-only",
        action="store_true",
        help="allow only the pinned static resource GET port; block direct upstream networking",
    )
    options = parser.parse_args()
    build(
        fresh_data=options.fresh_data,
        offline=options.offline,
        public_resources_only=options.public_resources_only,
    )
