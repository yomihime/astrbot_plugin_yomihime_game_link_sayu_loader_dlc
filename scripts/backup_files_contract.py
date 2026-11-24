"""Compare cache-file glob cleanup in fresh, disposable native and Loader folders."""

import argparse
import json
import logging
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

from native_contract import ROOT, REFERENCE_COMMIT, copy_reference, offline


def exercise(clear, root):
    root.mkdir(parents=True)
    (root / "nested.jpg").mkdir()
    for file in ("a.jpg", "b.png", "nested.jpg/c.jpg"):
        (root / file).write_text("fixture", "ascii")
    clear(root, "*.jpg")

    def inventory():
        return sorted(x.relative_to(root).as_posix() for x in root.rglob("*"))

    first = inventory()
    clear(root, "**/*.jpg")
    second = inventory()
    clear(root / "missing", "*.jpg")
    return [first, second]


def compare(work):
    sys.addaudithook(offline)
    sys.path.insert(0, str(work))
    from gsuid_core.utils.backup.backup_files import clear_path_all_file

    native = exercise(clear_path_all_file, work / "native-fixture")
    sys.path.insert(0, str(ROOT / "worker"))
    from plugin_backup import install_backup
    from port_errors import Unsupported

    fields = {}

    def package(name):
        fields[name] = SimpleNamespace()
        return fields[name]

    loader = work / "worker-fixture"
    install_backup(package, loader, "Example")
    clear = fields["gsuid_core.utils.backup.backup_files"].clear_path_all_file
    resources = loader / "resources" / "Example"
    actual = exercise(clear, resources / "test")
    for root, pattern in ((work, "*"), (resources, "*"), (resources / "test", "../*")):
        try:
            clear(root, pattern)
        except Unsupported:
            continue
        raise AssertionError("unexpected permission")
    expected = [["b.png", "nested.jpg", "nested.jpg/c.jpg"], ["b.png", "nested.jpg"]]
    if native != actual or native != expected:
        raise AssertionError((native, actual))
    return {"reference": REFERENCE_COMMIT, "remaining_names": actual, "denied": 3}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf8")
    sys.stderr.reconfigure(encoding="utf8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reference", type=Path, default=ROOT / ".sayu-local/reference"
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="cache-clean-") as tmp:
        work = Path(tmp)
        copy_reference(args.reference, work)
        try:
            result = compare(work)
        finally:
            logging.shutdown()
            sys.excepthook = sys.__excepthook__
    if args.output:
        args.output.write_text(json.dumps(result, indent=2), encoding="utf8")
    print(json.dumps(result))
