"""Index the pinned native Python surface and its static consumers without importing it."""

import argparse
import ast
import hashlib
import json
import zipfile
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
        digest = lock["snapshot_sha256"]
        archive = ROOT / ".sayu-local" / "runtime" / f"{lock['package']}-{digest}.zip"
        with archive.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise ValueError(f"{key} differs from its pinned archive")
            stream.seek(0)
            with zipfile.ZipFile(stream) as zipped:
                for name in zipped.namelist():
                    if not name.endswith(".py"):
                        continue
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", SyntaxWarning)
                        tree = ast.parse(zipped.read(name), filename=name)
                    for node in ast.walk(tree):
                        if isinstance(node, ast.ImportFrom) and (
                            node.module or ""
                        ).startswith("gsuid_core"):
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
        "trigger_contract": [
            "trigger.Trigger.check_command",
            "trigger.Trigger.get_command",
            "trigger.Trigger.gap_positions",
            "trigger.dispatch_sort_key",
        ],
        "layout_contract": [
            "server.GsServer.load_dir_plugins",
            "server.read_meta_plugin_info",
            "meta_plugins.require",
            "meta_plugins.provides_of",
            "meta_plugins.import_api",
            "meta_plugins.bind_meta_plugin_facade",
            "meta_plugins.unregister_meta_plugin",
            "meta_plugins.list_meta_plugins",
        ],
        "message_contract": [
            "models.Event",
            "models.Message",
            "models.MessageSend",
            "models.MessageReceive",
            "message_models.Button",
            "models.Event.get_meta",
            "models.Event.session_id",
            "segment.MessageSegment",
            "segment.process_buttons",
        ],
        "orm_contract": [
            "utils.database.base_models.Bind",
            "utils.database.base_models.BaseModel",
            "utils.database.base_models.User",
            "utils.database.base_models.with_session",
            "utils.database.startup.exec_list",
        ],
        "image_tools_contract": [
            "utils.image.image_tools." + name
            for name in (
                "crop_center_img",
                "easy_alpha_composite",
                "draw_text_by_line",
                "CustomizeImage",
                "draw_pic_with_ring",
                "get_color_bg",
                "get_event_avatar",
                "get_avatar_with_ring",
                "get_pic",
            )
        ],
        "mys_account_contract": [
            "utils.api.mys.base_request.BaseMysApi.endpoint_request",
            "utils.api.mys.base_request.BaseMysApi.get_server_id",
            "utils.api.mys.api.ApiEndpoint.get",
        ],
        "mys_cache_contract": ["utils.cache.gs_cache"],
        "mys_models_contract": ["utils.api.mys.models"],
        "subscribe_contract": ["subscribe.GsCoreSubscribe"],
        "help_contract": [
            "help.model.PluginHelp",
            "help.draw_new_plugin_help.get_new_help",
            "help.draw_plugin_help.get_help",
            "utils.error_reply.draw_error_img",
            "utils.error_reply.get_error_type",
            "utils.image.convert.get_str_size",
            "utils.image.convert.text2pic",
            "utils.image.mys.bbs.soup_to_img",
        ],
        "scheduler_contract": [
            "aps.get_all_aps_job",
            "aps.remove_repeat_job",
            "aps.add_scheduled_job",
            "aps.start_scheduler",
            "aps.shutdown_scheduler",
        ],
        "ai_contract": ["ai_core"],
        "app_life_contract": [
            "app_life",
            "utils.cookie_manager.qrlogin.get_qrcode_base64",
        ],
        "sign_contract": ["utils.sign.sign.sign_in"],
        "mys_bbs_contract": ["utils.api.mys.request.MysApi.get_bbs_post_detail"],
        "pool_contract": [
            "pool.to_thread",
            "pool.run_in_thread_pool",
            "pool.shutdown_pools",
        ],
        "inbound_adapter_contract": [
            "handler.handle_event",
            "handler.decode_inbound_frame",
        ],
        "user_language_contract": [
            "bot.Bot.get_lang",
            "bot.Bot.t",
            "utils.database.models.CoreUser.get_user_lang",
            "utils.database.models.CoreUser.set_user_lang",
        ],
        "minigg_contract": ["utils.api.minigg.request.minigg_request"],
        "core_config_log_contract": ["config.core_config", "logger.logger"],
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
