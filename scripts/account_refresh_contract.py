"""Compare the owner refresh handler in the pinned native Core and offline worker."""

import argparse
import asyncio
from contextlib import closing, nullcontext
import json
import os
from pathlib import Path
import subprocess
import sys
import sqlite3
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from native_contract import copy_reference, offline

ACTOR, OTHER, BOT = "fixture-owner", "other-owner", "adapter"
UID, SECOND, RAIL, THEIR = "100000071", "100000072", "100000073", "100000074"
INVALID, VALID = "stoken=v2_test", "stuid=7000077;stoken=test-only-session"


async def probe(mode, phase, worker):
    sys.addaudithook(offline)
    runtime = None
    if mode == "loader":
        sys.path.insert(0, str(worker))
        import sayu_compat as host
        from game_profile import PACKAGE, PREFIX
        from plugin_account_intake import IssuerUnavailable
        from plugin_bindings import binding_scope
        from plugin_runtime import PluginRuntime
        from service_settings import management_scope

        data = Path.cwd() / "data"
        data.mkdir(exist_ok=True)
        host.install(PACKAGE, PREFIX, data, source_root=Path.cwd())
        runtime = PluginRuntime(data)
        runtime.install(Path.cwd(), PACKAGE)
        with management_scope(writable=True):
            async with runtime.orm.startup():
                await runtime.orm.migrate()

        def scope(actor=ACTOR):
            return binding_scope(actor, BOT, True, uid_write=True, account_records=True)
    else:
        sys.path.insert(0, str(Path.cwd()))
        from gsuid_core.utils.database import base_models

        await base_models.init_database()
        from gsuid_core.utils.database.startup import ensure_core_database_tables

        await ensure_core_database_tables()

        def scope(actor=ACTOR):
            return nullcontext()

    from gsuid_core.utils.database.models import GsBind, GsUser
    from gsuid_core.utils.cookie_manager import add_ck
    from gsuid_core.buildin_plugins.core_command.user_login import send_refresh_ck_msg
    from gsuid_core.utils.error_reply import UID_HINT

    class Outbox:
        async def send(self, text):
            self.text = text

    async def refresh(actor=ACTOR):
        with scope(actor):
            if mode == "loader":
                result = await host.dispatch(
                    "core刷新CK",
                    actor_id=actor,
                    adapter_id=BOT,
                    conversation_kind="direct",
                )
                assert len(result) == 1 and set(result[0]) == {"text"}
                return result[0]["text"]
            bot = Outbox()
            await send_refresh_ck_msg(bot, SimpleNamespace(bot_id=BOT, user_id=actor))
            return bot.text

    fields = list(GsBind.__fields__)
    result = {"fields": fields}

    async def snapshot():
        rows = []
        for actor in (ACTOR, OTHER):
            with scope(actor):
                bindings = [
                    await GsBind.get_uid_list_by_game(actor, BOT, game)
                    for game in (None, "sr")
                ]
                users = await GsUser.select_data_list(actor, BOT)
                rows.append(
                    [
                        actor,
                        bindings,
                        sorted(
                            (
                                r.uid,
                                r.sr_uid,
                                r.stoken == INVALID,
                                r.stoken == VALID,
                                r.cookie == "fixture-cookie",
                            )
                            for r in users
                        ),
                    ]
                )
        return rows

    def sql_projection():
        # Read the temporary native/shared or per-actor worker SQLite files directly.
        # Emit only synthetic identities and classification of the test-only secrets.
        result = {"bindings": [], "accounts": [], "device_present": []}
        for path in Path.cwd().rglob("*"):
            if (
                path.suffix.lower() not in (".db", ".sqlite", ".sqlite3")
                or not path.is_file()
            ):
                continue
            with closing(
                sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            ) as connection:
                tables = {
                    r[0]
                    for r in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                binding_table = "gsbind" if mode == "native" else "bindings"
                if binding_table in tables:
                    result["bindings"].extend(
                        list(row)
                        for row in connection.execute(
                            f"SELECT user_id,bot_id,uid,sr_uid FROM {binding_table} WHERE bot_id=? AND user_id IN (?,?)",
                            (BOT, ACTOR, OTHER),
                        )
                    )
                if "gsuser" in tables:
                    for (
                        actor,
                        bot,
                        uid,
                        rail,
                        stoken,
                        cookie,
                        device,
                        fp,
                    ) in connection.execute(
                        "SELECT user_id,bot_id,uid,sr_uid,stoken,cookie,device_id,fp FROM gsuser WHERE bot_id=? AND user_id IN (?,?)",
                        (BOT, ACTOR, OTHER),
                    ):
                        assert stoken in (INVALID, VALID) and cookie == "fixture-cookie"
                        result["accounts"].append(
                            [actor, bot, uid, rail, stoken == INVALID, stoken == VALID]
                        )
                        result["device_present"].append([actor, bool(device), bool(fp)])
        return {k: sorted(v) for k, v in result.items()}

    try:
        if phase == "seed":
            assert await refresh() == UID_HINT
            result["no_uid_is_hint"] = True
            with scope():
                assert (
                    await add_ck.refresh_ck_by_uid_list(BOT, {}) == "请先绑定一个UID噢~"
                )
                assert await GsBind.insert_uid(ACTOR, BOT, UID) == 0
            result["single_no_stoken"] = await refresh()
            with scope():
                assert await GsBind.insert_uid(ACTOR, BOT, SECOND) == 0
                assert await GsBind.insert_uid(ACTOR, BOT, RAIL, game_name="sr") == 0
                await GsUser.full_insert_data(
                    user_id=ACTOR,
                    bot_id=BOT,
                    uid=SECOND,
                    stoken=INVALID,
                    cookie="fixture-cookie",
                )
            with scope(OTHER):
                assert await GsBind.insert_uid(OTHER, BOT, THEIR) == 0
                await GsUser.full_insert_data(
                    user_id=OTHER,
                    bot_id=BOT,
                    uid=THEIR,
                    stoken=VALID,
                    cookie="fixture-cookie",
                )
        before = await snapshot()
        result["sql"] = sql_projection()
        assert (
            len(result["sql"]["bindings"]) == 2 and len(result["sql"]["accounts"]) == 2
        )
        result["multi_malformed"] = await refresh()
        result["state"] = before
        assert await snapshot() == before
        assert (
            result["multi_malformed"]
            == "执行完成~成功刷新CK0个！跳过1个!\nUID100000072:可能是SK已过期~\n执行完成~成功刷新CK0个！跳过1个!"
        )
        if mode == "loader":
            from port_errors import Unsupported

            with scope():
                for bot, targets in [
                    (BOT, {UID: ACTOR, THEIR: OTHER}),
                    (BOT, {UID: ACTOR, THEIR: ACTOR}),
                    ("other-adapter", {UID: ACTOR}),
                ]:
                    with patch.object(
                        GsUser,
                        "get_user_stoken_by_uid",
                        side_effect=AssertionError("partial batch"),
                    ):
                        try:
                            await add_ck.refresh_ck_by_uid_list(bot, targets)
                        except Unsupported:
                            pass
                        else:
                            raise AssertionError("out-of-scope batch accepted")
                for call in (
                    add_ck.get_ck_by_stoken(BOT, OTHER),
                    asyncio.create_task(add_ck.get_ck_by_stoken(BOT, ACTOR)),
                ):
                    try:
                        await call
                    except Unsupported:
                        pass
                    else:
                        raise AssertionError("unexpected refresh grant")
            for private, grant, actor, adapter in [
                (False, True, ACTOR, BOT),
                (True, False, ACTOR, BOT),
                (True, True, OTHER, BOT),
                (True, True, ACTOR, "other-adapter"),
            ]:
                with binding_scope(actor, adapter, private, account_records=grant):
                    try:
                        await host.dispatch(
                            "core刷新CK",
                            actor_id=ACTOR,
                            adapter_id=BOT,
                            conversation_kind="direct",
                        )
                    except Unsupported:
                        pass
                    else:
                        raise AssertionError("unowned dispatch reached refresh")
            with scope():
                try:
                    await host.dispatch(
                        "core刷新全部CK",
                        actor_id=ACTOR,
                        adapter_id=BOT,
                        conversation_kind="direct",
                    )
                except (host.NoMatch, Unsupported):
                    pass
                else:
                    raise AssertionError(
                        "normal account grant reached administrative command"
                    )
            try:
                await refresh(OTHER)
            except IssuerUnavailable:
                result["issuer"] = "trusted issuer unavailable"
            else:
                raise AssertionError("account exchange unexpectedly available")
        else:
            import aiohttp

            calls = []

            class Rejected:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    return None

                async def json(self):
                    return {
                        "retcode": -100,
                        "message": "fixture issuer rejected",
                        "data": {},
                    }

            def reject(session, method, url, **kwargs):
                from urllib.parse import urlsplit

                calls.append([method, urlsplit(url).path])
                return Rejected()

            with patch.object(aiohttp.ClientSession, "request", reject):
                text = await refresh(OTHER)
            assert calls and text.startswith("执行完成~成功刷新CK0个！")
            result["issuer"] = calls
        assert await snapshot() == before
        after = sql_projection()
        assert (
            after["accounts"] == result["sql"]["accounts"]
            and after["bindings"] == result["sql"]["bindings"]
        )
        result["issuer_device_after"] = after["device_present"]
    finally:
        if runtime:
            await runtime.stop()
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    for arg in ("reference", "worker", "loader-python", "output"):
        ap.add_argument("--" + arg, type=Path)
    ap.add_argument("--phase", choices=["seed", "reopen"])
    ap.add_argument("--mode", choices=["native", "loader"])
    args = ap.parse_args()
    if args.mode:
        record = asyncio.run(probe(args.mode, args.phase, args.worker))
    else:
        import hashlib
        record = {}
        with tempfile.TemporaryDirectory() as temp:
            for mode, python in (
                ("native", sys.executable),
                ("loader", args.loader_python.resolve()),
            ):
                work = Path(temp) / mode
                work.mkdir()
                if mode == "native":
                    copy_reference(args.reference.resolve(), work)
                record[mode] = []
                for phase in ("seed", "reopen"):
                    dst = work / (phase + ".json")
                    env = {
                        k: v
                        for k, v in os.environ.items()
                        if k.upper()
                        in {"SYSTEMROOT", "WINDIR", "LANG", "PROCESSOR_ARCHITECTURE"}
                    }
                    env.update(TEMP=str(work), TMP=str(work), TMPDIR=str(work))
                    subprocess.run(
                        [
                            str(python),
                            "-B",
                            "-I",
                            "-X",
                            "utf8",
                            str(Path(__file__).resolve()),
                            "--mode",
                            mode,
                            "--phase",
                            phase,
                            "--worker",
                            str(args.worker.resolve()),
                            "--output",
                            str(dst),
                        ],
                        cwd=work,
                        env=env,
                        check=True,
                        timeout=90,
                    )
                    record[mode].append(json.loads(dst.read_bytes()))
        for a, b in zip(record["native"], record["loader"], strict=True):
            assert {
                k: v
                for k, v in a.items()
                if k not in ("issuer", "issuer_device_after", "sql")
            } == {
                k: v
                for k, v in b.items()
                if k not in ("issuer", "issuer_device_after", "sql")
            }
            for field in ("accounts", "bindings"):
                assert a["sql"][field] == b["sql"][field]
            assert a["issuer_device_after"] == b["issuer_device_after"]
        for phases in record.values():
            assert (
                phases[0]["issuer_device_after"] == phases[1]["sql"]["device_present"]
            )
            assert phases[0]["sql"]["accounts"] == phases[1]["sql"]["accounts"]
            assert phases[0]["sql"]["bindings"] == phases[1]["sql"]["bindings"]
        record["worker_sha256"] = hashlib.sha256(args.worker.read_bytes()).hexdigest()
    args.output.write_text(json.dumps(record, ensure_ascii=False, indent=2), "utf8")


if __name__ == "__main__":
    main()
