"""Compile the pinned framework translation data for offline workers."""

import ast
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import urlopen

COMMIT = "96cd7353be3100ff982bccc33e554961837c778e"
TREE = "e2041e500d638854d6cd8b6c5299f294ddeade30"
BASE = f"https://raw.githubusercontent.com/Genshin-bots/gsuid_core/{COMMIT}/gsuid_core/"
LOCALE_DIGEST = "9cd4c1eaefd6aa1d0d4d53a2e4b8802b4943be50f2808438de602cbf9853c2e5"
I18N_DIGEST = "5defe779a672795d845f8d0b3542ba6e938cbf3f393464170679f98dba18961a"
MEMBER = "plugin_locales.json"


def _fetch(url):
    with urlopen(url, timeout=25) as response:
        return response.read(2_000_000)


def verified_locales(reference=None):
    if reference is not None and Path(reference).is_dir():
        root = Path(reference) / "gsuid_core"
        files = {
            p.relative_to(root).as_posix(): p.read_bytes()
            for p in (root / "locales").rglob("*.json")
        }
        source = (root / "i18n.py").read_bytes()
    else:
        tree = json.loads(
            _fetch(
                f"https://api.github.com/repos/Genshin-bots/gsuid_core/git/trees/{TREE}?recursive=1"
            )
        )
        if tree["sha"] != TREE or tree["truncated"]:
            raise ValueError("unexpected locale tree")
        names = [
            "locales/" + entry["path"]
            for entry in tree["tree"]
            if entry["type"] == "blob"
        ]
        if not names or any(
            not re.fullmatch(r"locales/[a-z-]+/[a-z0-9_]+\.json", n) for n in names
        ):
            raise ValueError("unexpected locale path")
        with ThreadPoolExecutor(max_workers=8) as pool:
            files = dict(zip(names, pool.map(_fetch, [BASE + n for n in names])))
        source = _fetch(BASE + "i18n.py")
    digest = hashlib.sha256()
    catalogs = {}
    for name, raw in sorted(files.items()):
        digest.update(name.encode() + b"\0" + hashlib.sha256(raw).digest())
        catalog = catalogs.setdefault(Path(name).parent.name, {})
        catalog.update({k: v for k, v in json.loads(raw).items() if isinstance(v, str)})
    if (
        digest.hexdigest() != LOCALE_DIGEST
        or hashlib.sha256(source).hexdigest() != I18N_DIGEST
    ):
        raise ValueError("framework locale inputs differ from the pinned commit")
    # Read the native prefix vocabulary and its Unicode grammar as literals.
    # The framework's Python module is never imported into the worker.
    definitions = ast.parse(source).body
    emoji = next(
        ast.literal_eval(n.value)
        for n in definitions
        if isinstance(n, ast.AnnAssign)
        and isinstance(n.target, ast.Name)
        and n.target.id == "LOG_MODULE_EMOJI"
    )
    pattern = next(
        ast.literal_eval(n.value.args[0])
        for n in definitions
        if isinstance(n, ast.Assign)
        and isinstance(n.targets[0], ast.Name)
        and n.targets[0].id == "_LEADING_EMOJI_RE"
    )
    bundle = {"catalogs": catalogs, "emoji": emoji, "leading": pattern}
    return {
        MEMBER: json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).encode()
    }
