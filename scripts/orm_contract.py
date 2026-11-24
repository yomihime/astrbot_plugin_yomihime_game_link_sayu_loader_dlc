"""Run unmodified Waves database models against native GsCore and the packaged SQLite port."""

import argparse
import asyncio
from contextlib import nullcontext
import hashlib
import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build_local import worker_archive  # noqa: E402
from native_contract import REFERENCE_COMMIT, copy_reference, offline  # noqa: E402

PLUGIN = "XutheringWavesUID"


def load_models(source):
    # The subject is the complete database subpackage, including its real imports,
    # admin declarations and migration hook, isolated from unrelated game startup.
    outer = "plugins." + PLUGIN
    name = outer + "." + PLUGIN
    inner = PLUGIN + "/" + PLUGIN
    for path, relative in (
        ("plugins", ""),
        (outer, PLUGIN),
        (name, inner),
        (name + ".utils", inner + "/utils"),
    ):
        module = ModuleType(path)
        module.__path__ = [str(source / relative)]
        sys.modules[path] = module
    return importlib.import_module(name + ".utils.database.models")


async def business(models, base):
    from sqlalchemy import text

    result = []
    bind = models.WavesBind
    for uid in ("", "not-a-number", "1102", "1102", "1205"):
        result.append(await bind.insert_waves_uid("actor", "adapter", uid, "group"))
    result.append(await bind.get_uid_list_by_game("actor", "adapter"))
    result.append(await bind.switch_uid_by_game("actor", "adapter"))
    result.append(await bind.get_uid_by_game("actor", "adapter"))
    result.append([row.uid for row in await bind.get_binds_by_uid("1102")])
    result.append(
        await models.WavesUserActivity.update_user_activity("actor", "adapter", "robot")
    )
    result.append(
        await models.WavesUser.insert_data(
            "actor", "adapter", uid="1102", cookie="fixture"
        )
    )
    result.append(await models.WavesUser.get_user_cookie_by_uid("1102"))

    @base.with_session
    async def failed(cls, session):
        await session.execute(text("UPDATE WavesBind SET uid='uncommitted'"))
        raise ValueError("force rollback")

    @base.with_session
    async def nested(cls, session):
        try:
            await failed(cls)
        except ValueError:
            return await bind.get_uid_by_game("actor", "adapter")

    result.append(await nested(bind))
    try:
        await failed(bind)
    except ValueError:
        pass
    result.append(await bind.get_uid_by_game("actor", "adapter"))
    return result


async def presentation(models):
    language = models.WavesLangSettings
    values = [await language.get_lang("actor")]
    for selected in ("en", "", "kr"):
        await language.set_lang("actor", selected)
        values.append(await language.get_lang("actor"))
    return values


async def account_selection(*, native=False):
    from gsuid_core.utils.api.mys_api import mys_api
    from gsuid_core.utils.database.models import GsUID, GsUser

    owner = {"user_id": "actor", "bot_id": "adapter"}
    primary, spare = "100009001", "100009002"
    sr_primary, sr_spare = "100009003", "100009004"
    aliases = [str(100008001 + index) for index in range(4)]
    for suffix, uid, sr_uid in (
        ("primary", primary, sr_primary),
        ("spare", spare, sr_spare),
    ):
        await GsUser.full_insert_data(
            **owner,
            uid=uid,
            sr_uid=sr_uid,
            region="cn_gf01",
            sr_region="prod_gf_cn",
            cookie=f"fixture-cookie-{suffix}",
            stoken=f"fixture-stoken-{suffix}",
            fp=f"fixture-fp-{suffix}",
            device_id=f"fixture-device-{suffix}",
        )
    await GsUID.full_insert_data(
        main_uid=primary,
        game_name="gs",
        **{f"uid_{n}": alias for n, alias in enumerate(aliases, start=1)},
    )
    await GsUID.full_insert_data(main_uid=sr_spare, game_name="sr", uid_1=aliases[0])
    await GsUID.full_insert_data(main_uid=spare, game_name=None, uid_1=aliases[0])

    async def selected(uid, game, suffix):
        return {
            "owner": await mys_api.get_ck(uid, "OWNER", game)
            == f"fixture-cookie-{suffix}",
            "random": await mys_api.get_ck(uid, "RANDOM", game)
            == f"fixture-cookie-{suffix}",
            "stoken": await mys_api.get_stoken(uid, game) == f"fixture-stoken-{suffix}",
            "fp": await mys_api.get_user_fp(uid, game) == f"fixture-fp-{suffix}",
            "device": await mys_api.get_user_device_id(uid, game)
            == f"fixture-device-{suffix}",
        }

    checks = {}
    for index, alias in enumerate(aliases, start=1):
        assert await mys_api.get_uid(alias, "gs") == primary
        checks[f"gs_alias_{index}"] = await selected(alias, "gs", "primary")
    for name, game, canonical in (
        ("sr_alias", "sr", sr_spare),
        ("default_alias", None, spare),
    ):
        assert await mys_api.get_uid(aliases[0], game) == canonical
        checks[name] = await selected(aliases[0], game, "spare")
    checks["integer_uid"] = await selected(int(primary), "gs", "primary")
    assert all(all(result.values()) for result in checks.values())

    # The default values in GsUID.uid_2 through uid_4 are NULL. Native SQLite
    # matches those columns if passed Python None despite the API's str annotation.
    if native:
        assert await mys_api.get_uid(None, "sr") == sr_spare
        assert all((await selected(None, "sr", "spare")).values())
        absent_role = "matched_unset_alias"
    else:
        for read, args in (
            (mys_api.get_uid, (None, "sr")),
            (mys_api.get_ck, (None, "RANDOM", "sr")),
            (mys_api.get_ck, ("", "OWNER", "sr")),
            (mys_api.get_stoken, (None, "sr")),
            (mys_api.get_user_fp, (None, "sr")),
            (mys_api.get_user_device_id, (None, "sr")),
        ):
            try:
                await read(*args)
            except ValueError as exc:
                assert "role ID" in str(exc)
            else:
                raise AssertionError("account data was accessible without a role ID")
        absent_role = "role_id_required"

    async def cookie(uid, game):
        value = await mys_api.get_ck(uid, "RANDOM", game)
        if value is None:
            return None
        for name in ("primary", "spare"):
            if value == f"fixture-cookie-{name}":
                return name
        raise AssertionError("unexpected account selection")

    await GsUser.mark_invalid("fixture-cookie-primary", "expired")
    inactive = {
        "owner_keeps_own": await mys_api.get_ck(primary, "OWNER", "gs")
        == "fixture-cookie-primary",
        "stoken_keeps_own": await mys_api.get_stoken(primary, "gs")
        == "fixture-stoken-primary",
        "same_region": await cookie(aliases[1], "gs"),
        "sr_same_region": await cookie(sr_primary, "sr"),
        "default_fallback": await cookie(primary, None),
    }
    assert inactive == {
        "owner_keeps_own": True,
        "stoken_keeps_own": True,
        "same_region": "spare",
        "sr_same_region": "spare",
        "default_fallback": "spare",
    }
    # Native's global fallback also works for an unknown role. The Loader only
    # permits fallback when the caller has a row for that role in this private file.
    unknown = await cookie("100009999", "gs")
    assert unknown == ("spare" if native else None)
    await GsUser.update_data_by_uid_without_bot_id(
        spare, "gs", region="os_usa", status=""
    )
    inactive["wrong_region"] = await cookie(primary, "gs")
    inactive["sr_empty_status_same_region"] = await cookie(sr_primary, "sr")
    inactive["empty_status_without_region"] = await cookie(primary, None)
    assert inactive["wrong_region"] is inactive["empty_status_without_region"] is None
    assert inactive["sr_empty_status_same_region"] == "spare"
    await GsUser.update_data_by_uid_without_bot_id(primary, "gs", status=None)
    inactive["restored"] = await cookie(aliases[-1], "gs")
    assert inactive["restored"] == "primary"
    return {
        "same": {"aliases_and_fields": checks, "status": inactive},
        "unknown_role": unknown,
        "absent_role": absent_role,
    }


async def account_rows(*, native=False):
    from contextlib import ExitStack
    from unittest.mock import patch

    from gsuid_core.utils.api.mys.api import DAILY_NOTE
    from gsuid_core.utils.api.mys_api import mys_api
    from gsuid_core.utils.database.models import GsUID, GsUser
    from mys_account_contract import Capture, COOKIE, DEVICE, FP

    uid, alias = "100000917", "100000918"
    owner = dict(user_id="actor", bot_id="adapter")
    session = COOKIE
    assert await GsUser.base_select_data(**owner) is None
    await GsUser.full_insert_data(
        **owner,
        uid=uid,
        cookie=session,
        device_id=DEVICE,
        fp=FP,
        device_info="fixture/model",
    )
    await GsUID.full_insert_data(main_uid=uid, uid_1=alias, game_name="gs")
    row = await GsUser.base_select_data(**owner)
    capture = Capture()
    capture.payload = {"retcode": 0, "data": {"current_resin": 129}}
    with ExitStack() as stack:
        if native:
            stack.enter_context(capture.native_transport())
        else:
            import httpx2

            client = httpx2.AsyncClient
            stack.enter_context(
                patch.object(
                    httpx2,
                    "AsyncClient",
                    lambda **kw: client(
                        transport=httpx2.MockTransport(capture.worker_http), **kw
                    ),
                )
            )

        async def query():
            return await mys_api.endpoint_request(
                DAILY_NOTE,
                uid,
                params={"role_id": uid, "server": "cn_gf01"},
                ds_mode="none",
            )

        try:
            await query()
        except IndexError:
            assert not capture.calls
        else:
            raise AssertionError("malformed device data did not raise the native error")
        await GsUser.update_data_by_uid_without_bot_id(
            uid, device_info="fixture/model/os/build"
        )
        external = await query()
    assert external == capture.payload and len(capture.calls) == 1
    assert capture.calls[0]["method"] == "GET"
    assert capture.calls[0]["owner_cookie"] and not capture.calls[0]["signed"]
    assert capture.calls[0]["query"] == {"role_id": uid, "server": "cn_gf01"}
    values = [
        row.user_id,
        row.bot_id,
        row.device_info,
        await mys_api.get_uid(alias, "gs"),
        await mys_api.get_ck(alias, "OWNER", "gs") == session,
        await GsUser.get_user_stoken_by_uid(uid),
        await GsUser.update_data_by_uid_without_bot_id(
            uid, stoken="replacement-from-local-test"
        ),
        await mys_api.get_stoken(alias, "gs") == "replacement-from-local-test",
        await GsUser.delete_user_data_by_uid(uid),
        await mys_api.get_ck(alias, "OWNER", "gs"),
    ]
    assert values[:6] == ["actor", "adapter", "fixture/model", uid, True, None]
    assert values[6:] == [0, True, True, None]
    headers = capture.prepared[0]
    application = {
        k: v
        for k, v in headers.items()
        if k.startswith("x-") or k in ("referer", "origin", "user-agent")
    }
    return {
        "model": values,
        "wire": capture.calls[0],
        "application_headers": application,
        "malformed_device": "IndexError",
        "response": external,
    }


async def bind_lifecycle(bind):
    actor, adapter = "uid-owner", "uid-adapter"
    first, second = "000000284", "000000285"
    observations = [await bind.get_uid_by_game(actor, adapter)]
    for uid in (first, first, second):
        observations.append(
            await bind.insert_waves_uid(actor, adapter, uid, lenth_limit=9)
        )
    observations.append(await bind.get_uid_list_by_game(actor, adapter))
    observations.append(await bind.switch_uid_by_game(actor, adapter, second))
    observations.append(await bind.get_uid_by_game(actor, adapter))
    observations.append(await bind.delete_uid(actor, adapter, first))
    observations.append(await bind.delete_uid(actor, adapter, first))
    observations.append(await bind.delete_uid(actor, adapter, second))
    observations.append(await bind.get_uid_by_game(actor, adapter))
    observations.append(
        await bind.insert_waves_uid(actor, adapter, first, lenth_limit=9)
    )
    return observations


async def independent_preferences(orm):
    from contextlib import closing
    import sqlite3
    from gsuid_core.utils.database import base_models as base
    from plugin_bindings import binding_scope
    from port_errors import Unsupported
    from service_settings import management_scope
    from sqlmodel import Field

    class ReaderSettings(base.BaseIDModel, table=True):
        __module__ = "plugins.Reader.settings"
        user_id: str = Field(unique=True)
        locale: str

    class RoomSettings(base.BaseIDModel, table=True):
        __module__ = "plugins.Reader.settings"
        user_id: str = Field(unique=True)
        locale: str
        group_id: str

    class UnknownSettings(base.BaseIDModel, table=True):
        __module__ = "plugins.Reader.settings"
        user_id: str = Field(unique=True)
        locale: str
        payload: str

    class AccountSettings(base.BaseIDModel, table=True):
        __module__ = "plugins.Reader.settings"
        user_id: str = Field(unique=True)
        locale: str
        token: str

    class LibraryBind(base.Bind, table=True):
        __module__ = "plugins.Reader.models"
        uid: str | None = None
        ebook_uid: str | None = None

    class AccountBind(base.Bind, table=True):
        __module__ = "plugins.Reader.models"
        uid: str | None = None
        cookie: str = ""

    with management_scope(writable=True):
        async with orm.startup():
            await orm.migrate()

    @base.with_session
    async def add(cls, session, **values):
        session.add(cls(user_id="reader", locale="fr", **values))

    with binding_scope("reader", "local", True, uid_write=True):
        assert (
            await LibraryBind.insert_uid("reader", "local", "00102", game_name="ebook")
            == 0
        )
        assert await LibraryBind.get_uid_by_game("reader", "local", "ebook") == "00102"
        try:
            await AccountBind.insert_uid("reader", "local", "811")
        except Unsupported:
            pass
        else:
            raise AssertionError(
                "a mixed credential model received numeric UID authority"
            )
        path = orm.access().path
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("ALTER TABLE librarybind ADD COLUMN legacy_cookie TEXT")
        try:
            await LibraryBind.get_uid_by_game("reader", "local", "ebook")
        except Unsupported as exc:
            assert "differs from its declared model" in str(exc)
        else:
            raise AssertionError("an earlier mixed-purpose table received a UID grant")
        with closing(sqlite3.connect(path)) as db:
            assert db.execute("SELECT ebook_uid FROM librarybind").fetchone() == (
                "00102",
            )

    with binding_scope("reader", "local", True, preferences=True):
        await add(ReaderSettings)
        for model, values in (
            (RoomSettings, {"group_id": "room"}),
            (UnknownSettings, {"payload": "x"}),
            (AccountSettings, {"token": "fixture"}),
        ):
            try:
                await add(model, **values)
            except Unsupported:
                continue
            raise AssertionError(f"{model.__name__} received a preference grant")
        assert [m.__name__ for m in orm.preferences] == ["ReaderSettings"]
        row = await ReaderSettings.base_select_data(user_id="reader")
        assert row.locale == "fr"
        with closing(sqlite3.connect(orm.access().path)) as db, db:
            db.execute("ALTER TABLE readersettings ADD COLUMN legacy_secret TEXT")
        try:
            await add(ReaderSettings)
        except Unsupported as exc:
            assert "differs from its declared model" in str(exc)
        else:
            raise AssertionError("a stale physical table received a preference grant")
        return {
            "profile": "Reader",
            "table": ReaderSettings.__tablename__,
            "locale": row.locale,
            "binding": LibraryBind.__tablename__,
        }


async def child(kind, source, worker):
    sys.addaudithook(offline)
    work = Path.cwd()
    if kind == "native":
        sys.path.insert(0, str(work))
        from gsuid_core.utils.database import base_models as base

        await base.init_database()
        from gsuid_core.utils.database import startup

        models = load_models(source)
        await startup.create_core_tables()
        await startup.trans_adapter()
        await models._waves_auto_migrate()
        return {
            "observations": await business(models, base),
            "preferences": await presentation(models),
            "numeric_uids": await bind_lifecycle(models.WavesBind),
            "account_rows": await account_rows(native=True),
            "account_selection": await account_selection(native=True),
        }
    sys.path.insert(0, str(worker))
    import plugin_bindings
    from port_errors import Unsupported
    from plugin_runtime import PluginRuntime
    from sayu_compat import install
    from service_settings import management_scope
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    data = work / "private"
    plugin = "Reader" if kind == "independent" else PLUGIN
    install(plugin, "ww", data, source_root=source)
    runtime = PluginRuntime(data)
    runtime.install(source, plugin)
    if kind == "independent":
        return await independent_preferences(runtime.orm)
    orm = runtime.orm
    with management_scope(writable=True):
        async with orm.startup():
            models = load_models(source)
            first = await orm.migrate()
            await models._waves_auto_migrate()
            if kind == "loader":
                assert (
                    await models.WavesBind.insert_waves_uid(
                        "startup-seed", "adapter", "9393"
                    )
                    == 0
                )
    from gsuid_core.utils.database import base_models as base

    def actor(
        name="actor",
        *,
        adapter="adapter",
        private=True,
        writable=False,
        preferences=False,
        uid_read=True,
        uid_write=False,
        account_records=False,
    ):
        return plugin_bindings.binding_scope(
            name,
            adapter,
            private,
            writable=writable,
            preferences=preferences,
            uid_read=uid_read,
            uid_write=uid_write,
            account_records=account_records,
        )

    if kind == "reload":
        from gsuid_core.utils.api.mys_api import mys_api

        with actor():
            uid = await models.WavesBind.get_uid_by_game("actor", "adapter")
            assert await models.WavesLangSettings.get_lang("actor") == "kr"
            assert (
                uid == "1205"
                and first["files"] == 3
                and first["already_applied"] == 24 * first["files"]
                and first["applied"] == 0
            )
        with actor("uid-owner", adapter="uid-adapter"):
            assert (
                await models.WavesBind.get_uid_by_game("uid-owner", "uid-adapter")
                == "000000284"
            )
        with actor(account_records=True):
            assert (
                await mys_api.get_ck("100008001", "OWNER", "gs")
                == "fixture-cookie-primary"
            )
        for name, adapter in (("other", "adapter"), ("actor", "other")):
            with actor(name, adapter=adapter, account_records=True):
                assert await mys_api.get_uid("100008001", "gs") == "100008001"
                assert await mys_api.get_ck("100009001", "RANDOM", "gs") is None
                assert await mys_api.get_stoken("100009001", "gs") is None
                assert await mys_api.get_user_fp("100009001", "gs") is None
                assert await mys_api.get_user_device_id("100009001", "gs") is None
        return {
            "persistent_uid": uid,
            "persistent_numeric_uid": "000000284",
            "persisted_account_and_alias": True,
            "foreign_actor_and_adapter_isolated": True,
            "migration": first,
        }
    with actor(writable=True):
        assert await models.WavesBind.get_binds_by_uid("9393") == []
        observed = await business(models, base)

    @base.with_session
    async def increment(cls, session):
        before = (
            await session.execute(
                text("SELECT last_active_time FROM WavesUserActivity")
            )
        ).scalar_one()
        await asyncio.sleep(0.01)
        await session.execute(
            text("UPDATE WavesUserActivity SET last_active_time=:t"), {"t": before + 1}
        )
        return before

    async def independent_write():
        with actor(writable=True):
            return await increment(None)

    one, two = sorted(await asyncio.gather(independent_write(), independent_write()))
    assert two == one + 1

    async def must_deny(op):
        try:
            await op
        except Unsupported:
            return True
        raise AssertionError("operation unexpectedly succeeded")

    bind = models.WavesBind
    with actor("uid-owner", adapter="uid-adapter", uid_write=True):
        numeric_uids = await bind_lifecycle(bind)
        assert not plugin_bindings._current.get().writable

        @base.with_session
        async def raw_sql(cls, session, sql):
            await session.execute(text(sql))

        for sql in (
            "UPDATE wavesbind SET uid='１２３'",
            "UPDATE wavesbind SET uid='01_x'",
            "UPDATE wavesbind SET pgr_uid='token'",
            "UPDATE wavesbind SET group_id='group'",
            "UPDATE wavesbind SET bot_id='other'",
            "DELETE FROM wavesbind",
            "INSERT OR REPLACE INTO wavesbind (id,user_id,bot_id,uid) VALUES (1,'uid-owner','uid-adapter','45')",
            "SELECT cookie FROM wavesuser",
            "UPDATE subscribe SET uid='45'",
            "ALTER TABLE wavesbind ADD COLUMN cookie TEXT",
        ):
            await must_deny(raw_sql(bind, sql))
        await must_deny(raw_sql(None, "SELECT uid FROM wavesbind"))
        await must_deny(bind.get_uid_by_game("other", "uid-adapter"))
        await must_deny(bind.get_uid_by_game("uid-owner", "other"))
        await must_deny(
            asyncio.create_task(bind.get_uid_by_game("uid-owner", "uid-adapter"))
        )
        assert await bind.get_uid_by_game("uid-owner", "uid-adapter") == "000000284"
    with actor("uid-owner", adapter="uid-adapter", uid_read=False, uid_write=True):
        await must_deny(bind.get_uid_by_game("uid-owner", "uid-adapter"))
    with actor("uid-owner", adapter="uid-adapter"):
        await must_deny(bind.delete_uid("uid-owner", "uid-adapter", "000000284"))
    with actor("uid-owner", adapter="uid-adapter", private=False, uid_write=True):
        await must_deny(bind.get_uid_by_game("uid-owner", "uid-adapter"))
    for name, adapter in (("other", "uid-adapter"), ("uid-owner", "other")):
        with actor(name, adapter=adapter):
            assert await bind.get_uid_by_game(name, adapter) is None
    # Reopen rows created with the old administrative grant in their original
    # file, including an existing group association. Only UID columns may change.
    with actor(uid_write=True):
        previous = await bind.select_data("actor", "adapter")
        assert previous.group_id == "group"
        assert await bind.insert_waves_uid("actor", "adapter", "000002698") == 0
        assert await bind.delete_uid("actor", "adapter", "000002698") == 0
        assert (
            await bind.select_data("actor", "adapter")
        ).group_id == previous.group_id
    language = models.WavesLangSettings
    with actor(preferences=True):
        preferences = await presentation(models)

        @base.with_session
        async def raw_preference(cls, session, sql):
            await session.execute(text(sql))

        for sql in (
            "UPDATE WavesBind SET uid='wrong'",
            "SELECT cookie FROM WavesUser",
            "UPDATE CoreUser SET language='jp'",
            "UPDATE _sayu_migrations SET digest='wrong'",
            "INSERT INTO WavesLangSettings(user_id,lang) VALUES ('other','jp')",
            "UPDATE WavesLangSettings SET user_id='other'",
        ):
            await must_deny(raw_preference(language, sql))
        await must_deny(language.set_lang("other", "jp"))
        await must_deny(
            models.WavesUser.insert_data("actor", "adapter", uid="11", cookie="denied")
        )
        await must_deny(asyncio.create_task(language.set_lang("actor", "jp")))
        await must_deny(models.WavesUser.get_user_cookie_by_uid("11"))
    with actor(account_records=True):
        records = await account_rows()
        selection = await account_selection()
        assert await models.WavesUser.get_user_cookie_by_uid("1102") == "fixture"
        await must_deny(models.WavesUser.base_select_data(user_id="other"))
        await must_deny(
            asyncio.create_task(models.WavesUser.get_user_cookie_by_uid("1102"))
        )
    for name, adapter in (("other", "adapter"), ("actor", "other")):
        with actor(name, adapter=adapter, account_records=True):
            assert await models.WavesUser.get_user_cookie_by_uid("1102") is None
    with actor():
        await must_deny(language.set_lang("actor", "jp"))
        assert await language.get_lang("actor") == "kr"
    with actor("other"):
        assert await language.get_lang("other") == ""
    with actor(adapter="other"):
        assert await language.get_lang("actor") == ""
    with actor(private=False, preferences=True):
        await must_deny(language.set_lang("actor", "jp"))
    resume = asyncio.Event()

    async def deferred_write():
        await resume.wait()
        await language.set_lang("actor", "jp")

    with actor(preferences=True):
        deferred = asyncio.create_task(deferred_write())
    resume.set()
    await must_deny(deferred)

    limits = []
    with actor():
        assert await models.WavesBind.get_uid_by_game("actor", "adapter") == "1205"

        @base.with_session
        async def raw(cls, session):
            await session.execute(text("UPDATE WavesBind SET uid='forbidden'"))

        limits.append(await must_deny(raw(None)))
        limits.append(await must_deny(models.WavesBind.select_data("other", "adapter")))
        limits.append(
            await must_deny(
                asyncio.create_task(models.WavesBind.select_data("actor", "adapter"))
            )
        )
    for scope in (nullcontext(), actor(private=False)):
        with scope:
            limits.append(
                await must_deny(models.WavesBind.select_data("actor", "adapter"))
            )
    with actor("other"):
        assert await models.WavesBind.get_binds_by_uid("1205") == []
        assert await models.WavesBind.get_binds_by_uid("9393") == []
    with management_scope(writable=True):
        async with orm.startup():
            repeated = await orm.migrate()
            orm.migrations.extend(
                ["CREATE TABLE should_rollback (id INTEGER)", "INVALID SQL"]
            )
            try:
                await orm.migrate()
            except DBAPIError:
                assert orm.report["failed"] == 1
                failed = dict(orm.report)
            else:
                raise AssertionError("broken migration was accepted")
            async with orm.connect() as conn:
                assert not (
                    await conn.exec_driver_sql(
                        "SELECT name FROM sqlite_master WHERE name='should_rollback'"
                    )
                ).first()
            orm.migrations.pop()
            recovered = await orm.migrate()
    assert repeated["applied"] == 0 and repeated["files"] == 3
    with actor():
        assert await models.WavesBind.get_uid_by_game("actor", "adapter") == "1205"
    return {
        "observations": observed,
        "preferences": preferences,
        "numeric_uids": numeric_uids,
        "account_rows": records,
        "account_selection": selection,
        "owner_checks": len(limits),
        "concurrent_increments": 2,
        "migrations": [first, repeated, failed, recovered],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--child", choices=("loader", "native", "reload", "independent")
    )
    parser.add_argument("--source", type=Path)
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--loader-python", type=Path, default=Path(sys.executable))
    parser.add_argument(
        "--reference", type=Path, default=ROOT / ".sayu-local/reference"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.child:
        report = asyncio.run(child(args.child, args.source, args.worker))
    else:
        lock = json.loads((ROOT / "profiles/lock.json").read_bytes())["waves"]
        digest = lock["snapshot_sha256"]
        snap = ROOT / f".sayu-local/runtime/{PLUGIN}-{digest}.zip"
        assert hashlib.sha256(snap.read_bytes()).hexdigest() == digest
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            source, native, loader = (work / p for p in ("source", "native", "loader"))
            sources = copy_reference(args.reference.resolve(), native)
            loader.mkdir()
            (work / "independent").mkdir()
            with ZipFile(snap) as zipped:
                prefix = f"{PLUGIN}/{PLUGIN}/utils/database/"
                for name in zipped.namelist():
                    if name.startswith(prefix):
                        zipped.extract(name, source)
            archive = work / "worker.pyz"
            archive.write_bytes(worker_archive(lock))
            runs = {}
            for kind, interpreter in (
                ("loader", args.loader_python.resolve()),
                ("reload", args.loader_python.resolve()),
                ("independent", args.loader_python.resolve()),
                ("native", sys.executable),
            ):
                output = work / (kind + ".json")
                subprocess.run(
                    [
                        str(interpreter),
                        "-IB",
                        "-X",
                        "utf8",
                        __file__,
                        "--child",
                        kind,
                        "--source",
                        str(source),
                        "--worker",
                        str(archive),
                        "--output",
                        str(output),
                    ],
                    cwd=loader if kind == "reload" else work / kind,
                    check=True,
                    timeout=120,
                )
                runs[kind] = json.loads(output.read_bytes())
            report = {
                "reference": REFERENCE_COMMIT,
                "reference_files": sources,
                "source_sha256": digest,
                "worker_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                **runs,
            }
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if not args.child:
        if any(
            report["native"][k] != report["loader"][k]
            for k in ("observations", "preferences", "numeric_uids", "account_rows")
        ):
            raise SystemExit("native/loader model results differ")
        if (
            report["native"]["account_selection"]["same"]
            != report["loader"]["account_selection"]["same"]
        ):
            raise SystemExit("native/loader owner account selection differs")


if __name__ == "__main__":
    main()
