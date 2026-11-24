"""Inert role/card responses for the shared Core Cookie issuer and game consumers."""

from urllib.parse import urlsplit
from contextlib import contextmanager
import sys

UID = "600000071"
ACCOUNT = "700000077"
COOKIE = "TEST-ONLY-ACCOUNT-FIXTURE"


@contextmanager
def handler_trace(calls):
    names = {
        "send_qrcode_login",
        "send_add_ck_msg",
        "send_link_uid_msg",
        "send_daily_info",
        "insert_through_public_model",
    }
    previous = sys.getprofile()

    def observe(frame, event, arg):
        name = frame.f_code.co_name
        if event == "call" and name in names:
            qualified = frame.f_globals.get("__name__", "") + "." + name
            if qualified not in calls:
                calls.append(qualified)

    sys.setprofile(observe)
    try:
        yield
    finally:
        sys.setprofile(previous)


def scenarios(key, stored=False):
    prefix = {"genshin": "gs", "starrail": "sr"}[key]
    if stored:
        install_test_issuer(key)
        return [
            ("bind", "A", prefix + "绑定UID " + UID),
            ("insert_synthetic_row", "A", "fixturemyswrite"),
            ("status", "A", prefix + "当前状态"),
            ("other_status", "B", prefix + "当前状态"),
        ]
    return [
        ("qr_without_uid", "B", "扫码登录"),
        ("bind", "A", prefix + "绑定UID " + UID),
        ("invalid_cookie", "A", "core添加 fixture_invalid"),
        ("cookie", "A", f"core添加 account_id={ACCOUNT};cookie_token={COOKIE}"),
        ("status", "A", prefix + "当前状态"),
        ("other_status", "B", prefix + "当前状态"),
    ]


def install_test_issuer(key):
    """Test-only route. The ordinary Worker.handle account request owns the grant."""
    from gsuid_core.sv import SV
    from gsuid_core.utils.database.models import GsUser

    service = SV("Synthetic account fixture", area="DIRECT")

    @service.on_fullmatch("fixturemyswrite", prefix=False, block=True)
    async def insert_through_public_model(bot, ev):
        if ev.user_id != "A" or ev.bot_id != "onebot":
            raise PermissionError("unexpected test actor")
        roles = (
            {"uid": UID, "region": "os_usa"}
            if key == "genshin"
            else {"sr_uid": UID, "sr_region": "prod_official_usa"}
        )
        await GsUser.insert_data(
            user_id=ev.user_id,
            bot_id=ev.bot_id,
            mys_id=ACCOUNT,
            cookie=f"account_id={ACCOUNT};cookie_token={COOKIE}",
            **roles,
        )
        await bot.send("synthetic record inserted")


def reply(method, url, headers, key, calls):
    target = urlsplit(str(url))
    if target.hostname not in (
        "bbs-api-os.hoyolab.com",
        "sg-public-api.hoyolab.com",
        "sg-public-api.hoyoverse.com",
        "bbs-api-os.hoyoverse.com",
    ):
        return None
    owns_cookie = any(k.lower() == "cookie" and COOKIE in v for k, v in headers.items())
    calls.append(
        {
            "method": method,
            "host": target.hostname,
            "path": target.path,
            "synthetic_cookie": owns_cookie,
        }
    )
    if not owns_cookie:
        return {"retcode": -100, "message": "test account required"}
    if target.path.endswith("/getGameRecordCard"):
        return {
            "retcode": 0,
            "data": {
                "list": [{"game_id": 2 if key == "genshin" else 6, "game_role_id": UID}]
            },
        }
    if target.path.endswith("/dailyNote"):
        return {
            "retcode": 0,
            "data": {
                "current_resin": 174,
                "max_resin": 200,
                "current_expedition_num": 0,
                "max_expedition_num": 5,
                "expeditions": [],
                "transformer": {"recovery_time": {"reached": True}},
                "finished_task_num": 4,
                "total_task_num": 4,
                "is_extra_task_reward_received": True,
                "resin_discount_num_limit": 3,
                "remain_resin_discount_num": 2,
                "current_home_coin": 2400,
                "max_home_coin": 2400,
            },
        }
    if target.path.endswith("/note"):
        return {
            "retcode": 0,
            "data": {
                "current_stamina": 185,
                "max_stamina": 300,
                "stamina_recover_time": 0,
                "accepted_epedition_num": 0,
                "total_expedition_num": 4,
                "expeditions": [],
            },
        }
    return {"retcode": -1, "message": "not provided by the session fixture"}


def verify(profile, native, loader, stored, spec, reference):
    """Assert real responses, committed rows and the earliest native/Loader divergence."""
    assert (
        native["reference"] == reference
        and native["verified_blobs"] == native["manifest_blobs"]
    )
    assert loader["worker_origin"] == "frozen"
    for receipt in (native, loader):
        assert (
            receipt["commit"] == spec["commit"]
            and receipt["snapshot"] == spec["snapshot_sha256"]
        )
        assert (
            receipt["lifecycle"]["scheduler_started"]
            and receipt["lifecycle"]["scheduler_stopped"]
        )
    left = {r["case"]: r for r in native["cases"]}
    right = {r["case"]: r for r in loader["cases"]}
    keys = (
        ["bind", "insert_synthetic_row", "status", "other_status"]
        if stored
        else [
            "qr_without_uid",
            "bind",
            "invalid_cookie",
            "cookie",
            "status",
            "other_status",
        ]
    )
    assert list(left) == list(right) == keys
    allowed = {"status"} if stored else {"status", "cookie"}
    for side in (left, right):
        bound = issued = False
        for name, row in side.items():
            bound |= name == "bind"
            issued |= name == "insert_synthetic_row" or (
                name == "cookie" and side is left
            )
            assert row["state"]["bindings"] == ({"A": [UID]} if bound else {})
            assert row["state"]["credential_rows"] == (
                [["A", UID, True]] if issued else []
            )
            if side is right or name not in allowed:
                assert row["external_http"] == [] and row["http_details"] == [], (
                    name,
                    row,
                )
            package = spec["package"]
            module = (
                "genshinuid_resin" if profile == "genshin" else "starrailuid_stamina"
            )
            registration = {
                "qr_without_uid": "gsuid_core.buildin_plugins.core_command.user_login.send_qrcode_login",
                "invalid_cookie": "gsuid_core.buildin_plugins.core_command.user_login.send_add_ck_msg",
                "cookie": "gsuid_core.buildin_plugins.core_command.user_login.send_add_ck_msg",
                "insert_synthetic_row": "mys_package_session_fixture.insert_through_public_model",
                "bind": f"plugins.{package}.{package}.{('genshinuid_user' if profile == 'genshin' else 'starrailuid_user')}.send_link_uid_msg",
                "status": f"plugins.{package}.{package}.{module}.send_daily_info",
                "other_status": f"plugins.{package}.{package}.{module}.send_daily_info",
            }[name]
            if side is right and name in ("qr_without_uid", "invalid_cookie", "cookie"):
                assert row["executed_handlers"] == []
            else:
                assert row["executed_handlers"] == [registration], (
                    name,
                    row["executed_handlers"],
                )
            for part in row.get("parts", []):
                assert COOKIE not in str(part) and "cookie_token=" not in str(part)
        assert side["other_status"]["actor"] == "B"
        assert "还没有绑定过uid" in str(side["other_status"]["parts"])

    def request_record(path):
        return {
            "method": "GET",
            "host": "bbs-api-os.hoyolab.com",
            "path": path,
            "synthetic_cookie": True,
        }

    path = (
        "/game_record/genshin/api/dailyNote"
        if profile == "genshin"
        else "/game_record/hkrpg/api/note"
    )
    marker = "原粹树脂：174/200" if profile == "genshin" else "开拓力:185/300"
    assert left["status"]["http_details"] == [request_record(path)]
    assert marker in str(left["status"]["parts"])
    if stored:
        for side in (left, right):
            assert side["insert_synthetic_row"]["parts"] == [
                {"text": "synthetic record inserted"}
            ]
        failure = right["status"]
        assert failure["error"] == "NotImplementedError"
        stage, reason = (
            ("_send_owner", "account request authorization unavailable")
            if profile == "genshin"
            else (
                "_unbound_signature",
                "standalone account signatures require a trusted issuer",
            )
        )
        assert (
            failure["stack"][-1][0:2] == ["plugin_mys.py", stage]
            and failure["detail"] == reason
        )
        verdict = "stored_cookie_query_unavailable"
    else:
        assert "还没有绑定" in str(left["qr_without_uid"]["parts"])
        assert left["invalid_cookie"]["parts"][0]["image_bytes"] > 0
        assert left["cookie"]["parts"][0]["image_bytes"] > 0
        assert left["cookie"]["http_details"] == [
            request_record("/game_record/card/wapi/getGameRecordCard")
        ]
        for name in ("cookie", "invalid_cookie", "qr_without_uid"):
            assert right[name]["error"] == "NoMatch"
        assert "还没有绑定过Cookie" in str(right["status"]["parts"])
        verdict = "core_cookie_issuer_not_registered"
    return {
        "profile": profile,
        "status": verdict,
        "verified_steps": len(keys),
        "native_endpoint": path,
        "native_status": marker,
        "loader_http_calls": 0,
        "worker_sha256": loader["worker_sha256"],
        "raw_credentials_in_report": False,
    }
