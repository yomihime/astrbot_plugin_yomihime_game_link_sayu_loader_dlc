"""Compare protected inventory reads with actual fixed-core SQL queries on synthetic rows."""

import argparse
import asyncio
import hashlib
import json
import logging
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

from native_contract import ROOT, REFERENCE_COMMIT, copy_reference, offline


SAMPLES = [
    ("bindings", "get_all_data", ()),
    ("bindings", "get_all_uid_list_by_game", ("adapter",)),
    ("bindings", "get_all_uid_list_by_game", ("other", "gs")),
    ("bindings", "get_all_uid_list_by_game", ("adapter", "sr")),
    ("bindings", "get_all_uid_list_by_game", ("absent",)),
    ("bindings", "get_all_uid_list_by_game", ("adapter", "unknown")),
    ("users", "get_all_data", ()),
    ("users", "get_all_user", ()),
    ("users", "get_all_user", (False,)),
    ("users", "get_all_cookie", ()),
    ("users", "get_all_stoken", ()),
    ("users", "get_all_error_cookie", ()),
    ("users", "get_all_push_user_list", ()),
    ("users", "get_all_sign_user_list", ()),
    ("users", "get_push_user_list", ()),
    ("users", "get_sign_user_list", ()),
    ("users", "get_push_user_list", ("sr",)),
    ("users", "get_sign_user_list", ("zzz",)),
    ("users", "get_sign_user_list", ("unknown",)),
]


async def observe(pair):
    result = []
    for table, method, args in SAMPLES:
        model = pair[table]
        try:
            items = await getattr(model, method)(*args)
        except (ValueError, AttributeError) as exc:
            result.append(
                {
                    "table": table,
                    "method": method,
                    "args": args,
                    "exception": type(exc).__name__,
                }
            )
            continue
        values = []
        for item in items:
            if isinstance(item, str):
                values.append(item)
            else:
                assert isinstance(item, model)
                values.append(
                    item.model_dump() if hasattr(item, "model_dump") else vars(item)
                )
        digest = hashlib.sha256(
            json.dumps(values, sort_keys=True, default=str).encode()
        ).hexdigest()
        result.append(
            {
                "table": table,
                "method": method,
                "args": args,
                "rows": len(items),
                "ordered_result_sha256": digest,
            }
        )
    return result


async def compare():
    sys.addaudithook(offline)
    from gsuid_core.utils.database import base_models as db

    await db.init_database()
    from gsuid_core.utils.database.startup import ensure_core_database_tables
    from gsuid_core.utils.database.models import GsBind, GsUser
    from plugin_bindings import GsBind as BindingPort, binding_scope
    from plugin_mys import GsUser as AccountPort
    from plugin_enumeration import install_enumeration

    await ensure_core_database_tables()
    bindings = [
        GsBind(
            id=1,
            user_id="sample-one",
            bot_id="adapter",
            uid="first_second",
            sr_uid="rail",
        ),
        GsBind(id=2, user_id="sample-two", bot_id="adapter", uid="first"),
        GsBind(id=3, user_id="sample-one", bot_id="other", uid="third"),
        GsBind(id=4, user_id="sample-empty", bot_id="adapter"),
    ]
    users = [
        GsUser(
            id=1,
            user_id="a",
            bot_id="adapter",
            cookie="synthetic-cookie-a",
            stoken="synthetic-stoken-a",
            push_switch="on",
            sr_push_switch="on",
        ),
        GsUser(
            id=2,
            user_id="b",
            bot_id="other",
            cookie="synthetic-cookie-b",
            sign_switch="on",
            zzz_sign_switch="on",
        ),
        GsUser(
            id=3,
            user_id="c",
            bot_id="adapter",
            cookie="synthetic-cookie-a",
            stoken="synthetic-stoken-c",
        ),
        GsUser(
            id=4,
            user_id="d",
            bot_id="adapter",
            cookie="synthetic-cookie-invalid",
            status="invalid",
        ),
        GsUser(
            id=5,
            user_id="e",
            bot_id="adapter",
            cookie="",
            stoken="synthetic-stoken-alone",
        ),
    ]
    async with db.async_maker() as session:
        session.add_all([*bindings, *users])
        await session.commit()
        for record in [*bindings, *users]:
            await session.refresh(record)
    given = {
        "bindings": [r.model_dump() for r in bindings],
        "users": [r.model_dump() for r in users],
    }
    native = await observe({"bindings": GsBind, "users": GsUser})
    await db.engine.dispose()
    models = SimpleNamespace(GsBind=BindingPort, GsUser=AccountPort)
    authority = install_enumeration(
        models,
        admins={("admin", "test-adapter")},
        read_rows=given.__getitem__,
    )
    with (
        binding_scope("admin", "test-adapter", True, writable=True),
        authority.delivery(),
    ):
        loader = await observe({"bindings": models.GsBind, "users": models.GsUser})
    if native != loader:
        raise AssertionError(
            [(a, b) for a, b in zip(native, loader, strict=True) if a != b]
        )
    return loader


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf8")
    sys.stderr.reconfigure(encoding="utf8")
    with tempfile.TemporaryDirectory(prefix="sayu-enumeration-") as tmp:
        work = Path(tmp)
        copy_reference(ROOT / ".sayu-local/reference", work)
        sys.path[:0] = [str(work), str(ROOT / "worker")]
        try:
            cases = asyncio.run(compare())
        finally:
            logging.shutdown()
            sys.excepthook = sys.__excepthook__
    args.output.write_text(
        json.dumps(
            {
                "reference": REFERENCE_COMMIT,
                "worker_sha256": hashlib.sha256(
                    (ROOT / "worker/plugin_enumeration.py").read_bytes()
                ).hexdigest(),
                "cases": cases,
            },
            indent=2,
        ),
        "utf8",
    )
    print(f"Matched {len(cases)} native inventory query outcomes on synthetic rows.")
