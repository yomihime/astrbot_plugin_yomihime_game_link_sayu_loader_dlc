"""Build the framework's sign-result handling with a scoped host transport."""

import ast
from hashlib import sha256
from pathlib import Path
from urllib.request import urlopen

from image_assets import BASE

SOURCE = "gsuid_core/utils/sign/sign.py"
DIGEST = "f0e71fa731e3d34f75e63d8f9b1374a897083af6cf1bff5b549a0353ac126dc1"
PRELUDE = """# Genshin-bots/gsuid_core 96cd7353, gsuid_core/utils/sign/sign.py.
# GPL-3.0, see licenses/gsuid_core-COPYING.txt.
from gsuid_core.i18n import t
from gsuid_core.logger import logger
from plugin_sign import sign_error
from plugin_sign_wire import SignMysApi
mys_api = SignMysApi()

"""


def _source(reference, name, digest):
    if reference is not None and Path(reference).is_dir():
        stream = (Path(reference) / name).open("rb")
    else:
        stream = urlopen(BASE + name, timeout=30)
    with stream:
        data = stream.read(120_000)
    if sha256(data).hexdigest() != digest:
        raise ValueError("native sign source checksum mismatch")
    return ast.parse(data)


def _assignments(nodes, names):
    result = []
    for node in nodes:
        if isinstance(node, ast.Assign):
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign):
            target = node.target
        else:
            continue
        if isinstance(target, ast.Name) and target.id in names:
            result.append(node)
    if len(names) != len(result):
        raise ValueError("missing native sign request constants")
    return result


def _wire_module(reference):
    api = _source(
        reference,
        "gsuid_core/utils/api/mys/api.py",
        "d3c3d0544f18da993eedae0182aea4e9476024d929831558f0f6223cb2cc9887",
    )
    base = _source(
        reference,
        "gsuid_core/utils/api/mys/base_request.py",
        "e9323be47a61107954f5bc70db6a931b93de738c72f3a0c7c7a21e0794ca13c8",
    )
    req = _source(
        reference,
        "gsuid_core/utils/api/mys/sign_request.py",
        "0251c85c5ec6614e230ff046e8553c85e48cc6e44902920ee37e4a2c3e924fcd",
    )
    endpoints = _assignments(
        api.body,
        {
            "SIGN",
            "GS_BASE",
            "SIGN_SR",
            "SIGN_INFO",
            "SIGN_LIST",
            "SIGN_BASE_OS",
            "SIGN_INFO_SR",
            "SIGN_LIST_SR",
            "SIGN_INFO_ZZZ",
            "SIGN_SR_BASE_OS",
        },
    )
    tables = _assignments(
        req.body,
        {
            "_ACT_ID",
            "_GAME_NAME",
            "_BASE_URL",
            "_SIGN_END_POINT",
            "_SIGN_INFO_END_POINT",
            "_SIGN_LIST_END_POINT",
        },
    )
    base_class = next(
        i for i in base.body if isinstance(i, ast.ClassDef) and i.name == "BaseMysApi"
    )
    headers = _assignments(base_class.body, {"_HEADER", "_HEADER_OS"})
    req_class = next(
        i for i in req.body if isinstance(i, ast.ClassDef) and i.name == "SignMysApi"
    )
    funcs = [
        i
        for i in req_class.body
        if isinstance(i, ast.AsyncFunctionDef)
        and i.name in {"get_sign_info", "mys_sign", "get_sign_list"}
    ]
    if len(funcs) != 3:
        raise ValueError("missing native sign request functions")

    class SelectIssuer(ast.NodeTransformer):
        def visit_Call(self, node):
            if isinstance(node.func, ast.Name) and node.func.id in {
                "generate_os_ds",
                "get_web_ds_token",
            }:
                # Marker only. The scoped transport obtains an opaque test
                # signature from the caller for the final authorized request.
                return ast.Constant("os" if node.func.id == "generate_os_ds" else "web")
            return self.generic_visit(node)

    req_class.body = headers + [SelectIssuer().visit(fn) for fn in funcs]
    req_class.bases = [ast.Name(id="SignWireBase", ctx=ast.Load())]
    rules = """# Genshin-bots/gsuid_core 96cd7353; GPL-3.0.
from __future__ import annotations
from copy import deepcopy
from typing import Dict, cast, Any
from plugin_mys import ApiEndpoint, BaseMysApi
from plugin_sign import SignWireBase
mysVersion=BaseMysApi.mysVersion
SignInfo=SignList=MysSign=Any
"""
    return (
        rules
        + "\n".join(ast.unparse(node) for node in endpoints + tables + [req_class])
        + "\n"
    ).encode()


def verified_sign(reference=None):
    source = _source(reference, SOURCE, DIGEST)
    fn = next(
        n
        for n in source.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "sign_in"
    )
    names = next(
        n
        for n in source.body
        if isinstance(n, ast.Assign) and n.targets[0].id == "GAME_NAME_MAP"
    )
    # Only the native default challenge-failure path is part of this runtime.
    # Do not build any code that could solve a challenge or retry a protected request.
    conditions = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.If)
        and any(
            isinstance(s, ast.Constant) and s.value == "CaptchaPass"
            for s in ast.walk(n.test)
        )
    ]
    if len(conditions) != 1:
        raise ValueError("unexpected native challenge configuration branch")
    condition = conditions[0]
    condition.test = ast.Constant(False)
    condition.body = [ast.Pass()]
    body = PRELUDE + ast.unparse(names) + "\n\n" + ast.unparse(fn) + "\n"
    if "_pass(" in body or "CaptchaPass" in body:
        raise ValueError("unavailable challenge handler")
    return {
        "plugin_sign_result.py": body.encode(),
        "plugin_sign_wire.py": _wire_module(reference),
    }
