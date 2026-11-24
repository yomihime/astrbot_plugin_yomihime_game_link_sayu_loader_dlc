"""Exercise the sealed worker's Cookie validator and registration without credentials or network."""

import argparse
import asyncio
import ast
import hashlib
import base64
from io import BytesIO
from pathlib import Path
import secrets
import sys
import tempfile
from unittest.mock import AsyncMock, patch
from zipfile import ZipFile


def check_native_validator(archive):
    with ZipFile(archive) as worker:
        source = worker.read("plugin_cookie_manager/_native_input.py").decode("utf-8")
    original = next(
        node
        for node in ast.parse(source).body
        if getattr(node, "name", None) == "_deal_ck"
    )
    body = ast.get_source_segment(source, original).encode("utf-8")
    # Exact complete function in fixed GsCore 0.12.2, including its persistence path.
    assert (
        hashlib.sha256(body).hexdigest()
        == "6411e648d198ec3864e8d62ae8b4cf951a755b05bc2a233ca65c11429a17bded"
    )


async def check(archive, root):
    check_native_validator(archive)
    sys.path.insert(0, str(archive))
    from PIL import Image
    from game_profile import PACKAGE, PREFIX
    from plugin_account_intake import IssuerUnavailable
    from plugin_bindings import binding_scope
    from plugin_runtime import PluginRuntime
    from port_errors import Unsupported
    from worker_storage import install_offline_guard
    import sayu_compat as host

    install_offline_guard()
    source, data = root / "source", root / "data"
    source.mkdir()
    data.mkdir()
    runtime = PluginRuntime(data)
    host.install(PACKAGE, PREFIX, data, source_root=source)
    runtime.install(source, PACKAGE)
    try:
        from plugin_account_intake import install_account_services

        before_registration = host.registration_counts()
        install_account_services(host.package, host.private_service_registration)
        assert host.registration_counts() == before_registration
        from gsuid_core.sv import SV
        from gsuid_core.utils.cookie_manager.add_ck import _deal_ck_to_pic, deal_ck
        from gsuid_core.utils.database.models import GsBind
        from gsuid_core.utils.error_reply import UID_HINT

        token = secrets.token_hex(24)
        actor, adapter = "test-owner", "test-adapter"

        def scope():
            return binding_scope(actor, adapter, True, account_records=True)

        listener_hits = []

        @SV("Observer").on_message("probe")
        async def observer(bot, ev):
            listener_hits.append(True)

        async def receive(text, *, kind="direct", group=""):
            return await host.dispatch(
                text,
                actor_id=actor,
                adapter_id=adapter,
                conversation_kind=kind,
                group_id=group,
            )

        async def card(text, expected, prefix="core"):
            with scope():
                assert await deal_ck(adapter, text, actor, "TEXT") == (expected, 0)
                expected_bytes, score = await _deal_ck_to_pic(expected)
                result = await receive(prefix + "添加 " + text)
            assert score == 0 and len(result) == 1
            raw = base64.b64decode(result[0]["image"], validate=True)
            assert raw == expected_bytes and token.encode() not in raw
            with Image.open(BytesIO(raw)) as image:
                assert image.size == (800, 260)
                image.verify()

        await card("cookie_token=" + token, UID_HINT)
        with binding_scope(actor, adapter, True, uid_write=True):
            game = "sr" if PACKAGE == "StarRailUID" else None
            await GsBind.insert_uid(actor, adapter, "100000007", "", 9, game_name=game)
        await card(
            "test=" + token,
            "添加Cookies失败!\ncookies中应该包含cookie_token或者login_ticket相关信息!",
        )
        missing = "该CK字段出错, 缺少login_uid或stuid或ltuid字段!"
        for prefix, field in zip(
            ("core", "Core", "CORE"),
            ("stoken", "login_ticket", "cookie_token"),
            strict=True,
        ):
            await card(field + "=" + token, missing, prefix)
        await card("account_id=1;stoken=v2_" + token, "v2类型SK必须携带mid...")
        assert not listener_hits

        def files():
            return {
                p.relative_to(data): p.read_bytes()
                for p in data.rglob("*")
                if p.is_file()
            }

        before = files()
        for field in ("stoken", "login_ticket", "cookie_token", "cookie_token_v2"):
            with scope():
                try:
                    await receive("core添加 account_id=1;" + field + "=" + token)
                except IssuerUnavailable as exc:
                    assert token not in str(exc)
                else:
                    raise AssertionError("credential exchange unexpectedly succeeded")
        # A future issuer can return credentials in the native success summary.
        # The public wrapper must reject this before either text or pixels escape.
        from gsuid_core.utils.cookie_manager import add_ck, _native_input

        summary = "添加Cookies成功,account_id=1,cookie_token=" + token
        with (
            scope(),
            patch.object(
                _native_input, "_deal_ck", new=AsyncMock(return_value=summary)
            ),
            patch.object(add_ck, "_deal_ck_to_pic", new_callable=AsyncMock) as draw,
        ):
            for mode in ("TEXT", "PIC"):
                try:
                    await deal_ck(adapter, token, actor, mode)
                except Unsupported as exc:
                    assert token not in str(exc)
                else:
                    raise AssertionError(
                        "credential-bearing issuer output was delivered"
                    )
            draw.assert_not_called()
        assert before == files()
        assert not any(token.encode() in content for content in before.values())

        text = "core添加 test=" + token
        for name, bot, allow, kind, group in (
            (actor, adapter, False, "direct", ""),
            ("other", adapter, True, "direct", ""),
            (actor, "other", True, "direct", ""),
            (actor, adapter, True, "group", "room"),
            (actor, adapter, True, "", ""),
        ):
            with binding_scope(name, bot, True, account_records=allow):
                try:
                    await receive(text, kind=kind, group=group)
                except Unsupported:
                    pass
                else:
                    raise AssertionError(
                        "received account intake outside its owner scope"
                    )
        with scope():
            try:
                await asyncio.create_task(deal_ck(adapter, token, actor))
            except Unsupported:
                pass
            else:
                raise AssertionError("child task inherited account access")
        assert not listener_hits
        # A game-prefixed message still belongs to the ordinary game registry.
        try:
            await receive(PREFIX + "添加 ordinary")
        except host.NoMatch:
            pass
        assert listener_hits == [True]
    finally:
        await runtime.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        asyncio.run(check(args.worker.resolve(), Path(directory)))
    print(
        "Private intake cards, three native prefixes, credential exchange refusal and owner isolation passed."
    )


if __name__ == "__main__":
    main()
