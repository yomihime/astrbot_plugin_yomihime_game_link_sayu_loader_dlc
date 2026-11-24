"""Compare profile-local bindings with the same operations in a temporary native DB."""

import argparse
import asyncio
from contextlib import nullcontext
import hashlib
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
ACTOR = "fixture-actor"
BOT = "fixture-adapter"


async def account_input_probe():
    from gsuid_core.utils.api.mys import base_request
    from gsuid_core.utils.cookie_manager import add_ck
    from gsuid_core.utils.database import utils as db_regions

    BaseMysApi = base_request.BaseMysApi
    assert db_regions.SERVER is base_request.RECOGNIZE_SERVER
    assert db_regions.SERVER is BaseMysApi.RECOGNIZE_SERVER
    assert db_regions.SR_SERVER is base_request.SR_SERVER
    assert db_regions.ZZZ_SERVER is base_request.ZZZ_SERVER
    region_fields = {
        name: list(getattr(db_regions, name).items())
        for name in ("SERVER", "SR_SERVER", "ZZZ_SERVER")
    }

    cases = [
        "",
        "unused=value;login_ticket=transient",
        "stuid=third;account_id=second;login_uid=first",
        'login_uid="";account_id=second',
        'account_id_v2=actor;cookie_token_v2="test=value";ltoken_v2=local',
        "ltmid_v2=second;mid=first;stoken=test;custom=drop;ltoken=",
        "stoken_v2=test;cookie_token=sample;login_ticket_v2=drop",
    ]
    # Include every public field in one input, independently of the implementation.
    cases.append(
        ";".join(
            f"{key}{suffix}=test{index}"
            for index, key in enumerate(
                (
                    "login_uid",
                    "account_id",
                    "stuid",
                    "ltuid",
                    "stoken",
                    "cookie_token",
                    "login_ticket",
                    "ltoken",
                    "account_mid",
                    "ltmid",
                    "stmid",
                )
            )
            for suffix in ("", "_v2")
        )
        + ";mid=test"
    )
    cookies = []
    for text in cases:
        parsed = SimpleCookie(text)
        persist = add_ck.get_persist_cookie_data(parsed)
        cookies.append(
            [
                await add_ck.get_account_id(parsed),
                add_ck.get_cookie_value(parsed, ["mid", "ltmid_v2"]),
                list(persist.items()),
                add_ck.serialize_cookie(persist),
            ]
        )
    regions = []
    for roles in [
        (None, None, None),
        ("1", "7", None),
        ("6", "1", None),
        (None, "5", "1234567890"),
        ("", "6", None),
        ("", "", "123456789"),
        ("", None, "1234567890"),
        ("x", "6", None),
    ]:
        try:
            regions.append(add_ck.is_overseas_account(*roles))
        except ValueError as exc:
            regions.append(type(exc).__name__)
    devices = [
        (
            BaseMysApi.get_overseas_device_id(actor),
            BaseMysApi.get_overseas_device_fp(actor),
        )
        for actor in (42, "42", "0042", "", "测试")
    ]
    return {
        "cookies": cookies,
        "regions": regions,
        "region_fields": region_fields,
        "devices": devices,
    }


async def account_status_cards():
    from gsuid_core.utils.cookie_manager.add_ck import draw_add_ck_pic, _deal_ck_to_pic
    from gsuid_core.utils.image.image_tools import get_status_icon, get_v4_bg

    def pixels(img):
        with img:
            return (img.size, img.mode, hashlib.sha256(img.tobytes()).hexdigest())

    backgrounds = [
        pixels(get_v4_bg(96, 64, dark, blur))
        for dark in (False, True)
        for blur in (False, True)
    ]
    icons = [pixels(get_status_icon(value)) for value in (False, True, -1)]
    cards = []
    # Synthetic status messages only. Each item exercises the actual renderer
    # and encoder from this runtime, including the exact 27-character boundary.
    for message in (
        "",
        "请先绑定游戏角色",
        "甲" * 27 + "\n" + "乙" * 26,
        "甲" * 26 + "\n" + "乙" * 27 + "\n第三行",
        "添加Cookies成功\n合成说明",
        "添加Stoken成功\n合成说明\n添加Cookies成功",
    ):
        img, count = await draw_add_ck_pic(message)
        encoded, encoded_count = await _deal_ck_to_pic(message)
        assert count == encoded_count == message.count("成功")
        cards.append((pixels(img), count, hashlib.sha256(encoded).hexdigest()))
    return {"backgrounds": backgrounds, "icons": icons, "cards": cards}


async def enrollment_without_exchange(inputs):
    from gsuid_core.utils.cookie_manager.add_ck import deal_ck

    receipt = []
    for value in inputs:
        text, status = await deal_ck(BOT, value, ACTOR, "TEXT")
        card, picture_status = await deal_ck(BOT, value, ACTOR)
        assert status == picture_status == 0
        assert "fixture-enroll" not in text
        receipt.append((text, status, hashlib.sha256(card).hexdigest()))
    return receipt


async def probe(mode, phase, worker):
    from native_contract import offline

    sys.addaudithook(offline)
    if mode == "loader":
        sys.path.insert(0, str(worker))
        from plugin_bindings import binding_scope
        from plugin_runtime import PluginRuntime
        import sayu_compat

        data = Path.cwd() / "data"
        data.mkdir(exist_ok=True)
        sayu_compat.install("Example", "ex", data, source_root=Path.cwd())
        PluginRuntime(data).install(Path.cwd(), "Example")
        scope = binding_scope(ACTOR, BOT, True, writable=True, account_records=True)
    else:
        sys.path.insert(0, str(Path.cwd()))
        from gsuid_core.utils.database import base_models
        from gsuid_core.utils.database.models import GsBind

        await base_models.init_database()
        async with base_models.engine.begin() as connection:
            await connection.run_sync(GsBind.__table__.create, checkfirst=True)
        scope = nullcontext()
    from gsuid_core.utils.database.models import GsBind
    from gsuid_core.utils.database.api import get_uid
    from gsuid_core.utils.cookie_manager.add_ck import get_all_bind_uid

    result = {}
    with scope:
        result["account_input"] = await account_input_probe()
        result["roles_before"] = await get_all_bind_uid(BOT, ACTOR)
        if phase == "write":
            result["status_cards"] = await account_status_cards()
            result["no_binding_intake"] = await enrollment_without_exchange(
                ["account_id=42;cookie_token=fixture-enroll"]
            )
            result["insert"] = [
                await GsBind.insert_uid(ACTOR, BOT, value, "room", 9)
                for value in ["0", "notnumber", "000000001", "000000002", "000000001"]
            ]
            result["sr"] = await GsBind.insert_uid(
                ACTOR, BOT, "000000003", "room", 9, game_name="sr"
            )
            for game, uid in (("zzz", "000000005"), ("bbb", "000000006")):
                await GsBind.insert_uid(ACTOR, BOT, uid, "room", 9, game_name=game)
            result["invalid_intake"] = await enrollment_without_exchange([
                "other=fixture-enroll",
                "stoken=fixture-enroll",
                "login_ticket=fixture-enroll",
                "cookie_token=fixture-enroll",
                "account_id=42;stoken=v2_fixture-enroll",
            ])
            result["switch_missing"] = await GsBind.switch_uid_by_game(
                ACTOR, BOT, "000000004"
            )
            result["rotate"] = await GsBind.switch_uid_by_game(ACTOR, BOT)
            result["set_group"] = await GsBind.update_data(
                ACTOR, BOT, group_id="room2_room3"
            )
        else:
            result["delete_missing"] = await GsBind.delete_uid(ACTOR, BOT, "000000004")
            result["delete"] = await GsBind.delete_uid(ACTOR, BOT, "000000001")
            result["switch_last"] = await GsBind.switch_uid_by_game(ACTOR, BOT)
        result["roles_after"] = await get_all_bind_uid(BOT, ACTOR)
        result["list"] = await GsBind.get_uid_list_by_game(ACTOR, BOT)
        result["sr_value"] = await GsBind.get_uid_by_game(ACTOR, BOT, "sr")
        result["group"] = (await GsBind.select_data(ACTOR, BOT)).group_id
        result["implicit_adapter"] = (await GsBind.select_data(ACTOR)).group_id
        row = await GsBind.select_data(ACTOR, BOT)
        assert isinstance(row, GsBind)
        result["materialized_row"] = row.model_dump()
        result["explicit_fields"] = sorted(row.model_fields_set)
        assert json.loads(row.model_dump_json()) == result["materialized_row"]
        from sqlalchemy import inspect

        result["detached"] = inspect(row).detached
        result["row_identity"] = list(inspect(row).identity)
        result["table_columns"] = list(GsBind.__table__.columns.keys())
        result["constructed_empty"] = GsBind().model_dump()
        row.group_id = "local change"
        assert (await GsBind.select_data(ACTOR, BOT)).group_id == result["group"]
        result["exists"] = await GsBind.bind_exists(ACTOR, BOT)
        result["first_group"] = await GsBind.get_bind_group(ACTOR, BOT)
        result["groups"] = await GsBind.get_bind_group_list(ACTOR, BOT)
        event = SimpleNamespace(text="", user_id=ACTOR, at=None, bot_id=BOT)
        result["stored"] = await get_uid(None, event, GsBind, None, True)
        event.text = "input 000000007"
        result["explicit"] = await get_uid(None, event, GsBind, None, True)
    if mode == "loader":
        with binding_scope(ACTOR, BOT, True):
            await GsBind.get_uid_by_game(ACTOR, BOT)
            try:
                await GsBind.delete_uid(ACTOR, BOT, "000000002")
            except NotImplementedError:
                pass
            else:
                raise AssertionError("read-only route permitted a UID mutation")
        for private, actor, adapter in [
            (False, ACTOR, BOT),
            (True, "other", BOT),
            (True, ACTOR, "other"),
            (True, "", BOT),
        ]:
            with binding_scope(actor, adapter, private):
                for operation in (
                    GsBind.get_uid_by_game(ACTOR, BOT),
                    GsBind.delete_uid(ACTOR, BOT, "000000002"),
                    get_all_bind_uid(BOT, ACTOR),
                ):
                    try:
                        await operation
                    except NotImplementedError:
                        pass
                    else:
                        raise AssertionError("owner data escaped its invocation scope")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--loader-python", type=Path, default=Path(sys.executable))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--mode", choices=["native", "loader"], help=argparse.SUPPRESS)
    parser.add_argument("--phase", choices=["write", "reopen"], help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.mode:
        result = asyncio.run(probe(args.mode, args.phase, args.worker))
    else:
        from native_contract import REFERENCE_COMMIT, copy_reference

        result = {"reference": REFERENCE_COMMIT}
        with tempfile.TemporaryDirectory(prefix="sayu-bindings-") as folder:
            from build_local import worker_archive

            worker = args.worker
            if worker is None:
                worker = Path(folder) / "worker.pyz"
                lock = json.loads((ROOT / "profiles/lock.json").read_bytes())
                worker.write_bytes(worker_archive(lock["starrail"], offline=True))
            result["worker_sha256"] = hashlib.sha256(worker.read_bytes()).hexdigest()
            for mode in ["native", "loader"]:
                work = Path(folder) / mode
                work.mkdir()
                if mode == "native":
                    copy_reference(args.reference.resolve(), work)
                phases = []
                for phase in ["write", "reopen"]:
                    log = work / (phase + ".json")
                    command = [
                        str(args.loader_python.resolve()) if mode == "loader" else sys.executable,
                        "-I",
                        "-B",
                        "-X",
                        "utf8",
                        str(Path(__file__).resolve()),
                        "--mode",
                        mode,
                        "--worker",
                        str(worker.resolve()),
                        "--phase",
                        phase,
                        "--output",
                        str(log),
                    ]
                    env = {
                        k: v
                        for k, v in os.environ.items()
                        if k.upper() in {"SYSTEMROOT", "WINDIR", "LANG"}
                    }
                    env.update(TMP=str(work), TEMP=str(work), TMPDIR=str(work))
                    subprocess.run(command, cwd=work, env=env, check=True, timeout=45)
                    phases.append(json.loads(log.read_bytes()))
                result[mode] = phases
        if result["native"] != result["loader"]:
            raise AssertionError(result)
        result["owner_denials_verified"] = True
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
