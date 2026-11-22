"""Index the pinned native Python surface and its static consumers without importing it."""

import argparse
import ast
import hashlib
import json
import subprocess
import warnings
from collections import defaultdict
from pathlib import Path

REFERENCE = "96cd7353be3100ff982bccc33e554961837c778e"
SOURCE_INDEX = "c538fa380d3b5ed12afc013be0e6218d1f75e47eb33d19c3fab87b229b487ff0"
ROOT = Path(__file__).resolve().parents[1]


def parse(path):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        return ast.parse(path.read_bytes(), filename=path.name)


def files(root):
    entries = []
    for path in sorted((root / "gsuid_core").rglob("*.py")):
        if "plugins" in path.relative_to(root).parts[:2]:
            continue  # Native test runners may stage a plugin here.
        data = path.read_bytes().replace(b"\r\n", b"\n")
        sha = hashlib.sha1(
            b"blob " + str(len(data)).encode() + b"\0" + data
        ).hexdigest()
        entries.append((path.relative_to(root).as_posix(), sha))
    payload = "".join(f"{path} {sha}\n" for path, sha in sorted(entries))
    if hashlib.sha256(payload.encode()).hexdigest() != SOURCE_INDEX:
        raise ValueError(
            f"GsCore sources differ from {REFERENCE} ({len(entries)} files)"
        )
    return [root / name for name, _ in sorted(entries)]


def consumers():
    result = defaultdict(set)
    locks = json.loads((ROOT / "profiles/lock.json").read_bytes())
    for key, lock in locks.items():
        repo = ROOT / ".sayu-local" / key / lock["directory"]

        def git(*args):
            return subprocess.check_output(["git", "-C", str(repo), *args])

        if git("rev-parse", "HEAD").decode().strip() != lock["commit"]:
            raise ValueError(f"{key} differs from pinned revision")
        git("diff", "--quiet", "HEAD", "--", "*.py")
        for name in git("ls-files", "-z", "*.py").decode().split("\0"):
            if not name:
                continue
            for node in ast.walk(parse(repo / name)):
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                    "gsuid_core"
                ):
                    for item in node.names:
                        result[f"{node.module}.{item.name}"].add(key)
                if isinstance(node, ast.Import):
                    for item in node.names:
                        if item.name.startswith("gsuid_core"):
                            result[item.name].add(key)
    return result


def symbols(body, prefix=""):
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_") or node.name in {"__init__", "__call__"}:
                name = prefix + node.name
                yield name, node.lineno, type(node).__name__
                if isinstance(node, ast.ClassDef):
                    yield from symbols(node.body, name + ".")
        elif isinstance(node, (ast.AnnAssign, ast.Assign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    yield prefix + target.id, node.lineno, "attribute"
        elif isinstance(node, ast.ImportFrom) and not prefix:
            for item in node.names:
                name = item.asname or item.name
                if not name.startswith("_"):
                    yield name, node.lineno, "import"
        elif isinstance(node, (ast.If, ast.Try)):
            yield from symbols(node.body, prefix)
            yield from symbols(node.orelse, prefix)


def proof_index():
    # An entry names the probe that exercises part of a symbol's surface.
    # It never promotes untested arguments or inherited APIs to verified status.
    probes = {
        "native_contract": [
            "server.GsServer.load_plugin",
            "server.GsServer.cached_import",
            "server.on_core_start",
            "server.on_core_shutdown",
            "aps.scheduler",
            "sv.SV.on_fullmatch",
            "sv.SV.on_command",
            "handler.msg_process",
            "bot.Bot.send",
            "data_store.get_res_path",
        ],
        "service_contract": [
            "sv.SV.set",
            "sv.Plugins.set",
            "sv.SV.enable",
            "sv.SV.disable",
        ],
        "binding_contract": [
            "utils.database.api.get_uid",
            *(
                "utils.database.models.GsBind." + method
                for method in (
                    "insert_uid",
                    "delete_uid",
                    "switch_uid_by_game",
                    "get_uid_by_game",
                    "get_uid_list_by_game",
                    "update_data",
                )
            ),
        ],
        "config_contract": [
            "utils.plugins_config.gs_config.StringConfig." + method
            for method in ("__init__", "get_config", "set_config", "get_raw_config")
        ],
        "target_contract": [
            "bot.Bot.target_send",
            "bot.Bot.receive_resp",
            "bot.Bot.receive_mutiply_resp",
        ],
    }
    return {
        "gsuid_core." + symbol: "scripts/" + script + ".py"
        for script, names in probes.items()
        for symbol in names
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path)
    args = parser.parse_args()
    sources = files(args.reference)
    uses = consumers()
    proofs = proof_index()
    print("symbol\tkind\tsource\tline\texplicit_plugin_imports\tbehavior_probe")
    for path in sources:
        rel = path.relative_to(args.reference)
        module = ".".join(rel.with_suffix("").parts).removesuffix(".__init__")
        for name, line, kind in [("", 1, "module"), *symbols(parse(path).body)]:
            symbol = module + ("." + name if name else "")
            print(
                symbol,
                kind,
                rel.as_posix(),
                line,
                ",".join(sorted(uses[symbol])),
                proofs.get(symbol, "unverified"),
                sep="\t",
            )


if __name__ == "__main__":
    main()
