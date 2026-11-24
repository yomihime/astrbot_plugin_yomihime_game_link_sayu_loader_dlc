"""Package the complete native account validator and its private command registration."""

import ast

from sign_assets import verified_source


COOKIE = "gsuid_core/utils/cookie_manager/add_ck.py"
BUILTIN = "gsuid_core/buildin_plugins/core_command/"
GENERATED = "# Genshin-bots/gsuid_core 96cd7353, GPL-3.0. See licenses/gsuid_core-COPYING.txt.\n"


def _render(prelude, statements):
    return (
        GENERATED
        + prelude
        + "\n"
        + "\n\n".join(ast.unparse(statement) for statement in statements)
        + "\n"
    ).encode("utf-8")


def verified_cookie_input(reference):
    cookie = verified_source(
        reference,
        COOKIE,
        "6fa204b767b2c9f9038a4a83f623c10a02e4935b72ad5b54dc80d17e898ea1e6",
    )
    intake = next(
        node
        for node in ast.parse(cookie).body
        if getattr(node, "name", None) == "_deal_ck"
    )
    lines = cookie.decode("utf-8").splitlines(keepends=True)
    original = "".join(lines[intake.lineno - 1 : intake.end_lineno])
    # The native success description includes credentials. Only literal failure
    # messages are safe to display until the host can project issuer outcomes.
    literal_errors = sorted(
        {
            node.value.value
            for node in ast.walk(intake)
            if isinstance(node, ast.Return)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        }
    )

    plugin = ast.parse(
        verified_source(
            reference,
            BUILTIN + "__init__.py",
            "7eadbf3ad5729279014ecfd4187f3cede527a0b3c2cac7b0523fe20cb9b025d6",
        )
    )
    login = ast.parse(
        verified_source(
            reference,
            BUILTIN + "user_login/__init__.py",
            "522574b2f16dbe793c69c70d18acb20315b0c581020da8d14e0d8b036a36f56a",
        )
    )
    service = next(
        node
        for node in login.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "sv_core_user_addck"
    )
    handlers = [
        node
        for node in login.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name in ("_send_help", "send_add_ck_msg")
    ]
    if len(handlers) != 2:
        raise ValueError("native account intake handlers missing")
    return {
        "plugin_cookie_manager/_native_input.py": (
            GENERATED
            + """from http.cookies import SimpleCookie
from typing import Dict, List
from gsuid_core.utils.database.utils import SERVER, SR_SERVER, ZZZ_SERVER
from gsuid_core.utils.database.models import GsUser, GsUID, GsCache
from gsuid_core.utils.error_reply import UID_HINT
from gsuid_core.utils.cookie_manager.add_ck import (
    get_all_bind_uid, is_overseas_account, get_persist_cookie_data,
    get_account_id, get_cookie_value, serialize_cookie, sk_list, ck_list, lt_list, mid_list,
)
from plugin_account_intake import intake_issuer as mys_api
"""
            + f"PUBLIC_ERRORS = frozenset({literal_errors!r}) | {{UID_HINT}}\n\n"
            + original
        ).encode("utf-8"),
        "native_account_services/core_command/__init__.py": _render("", plugin.body),
        "native_account_services/core_command/user_login.py": _render(
            """from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.message_models import Button
from gsuid_core.utils.cookie_manager.add_ck import deal_ck
""",
            [service, *handlers],
        ),
    }
