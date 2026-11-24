"""Package the pinned native SQLModel declarations, independently of GsCore's DB driver."""

import ast
import builtins
import hashlib
import symtable
from pathlib import Path
from urllib.request import urlopen

from image_assets import BASE

PATH = "gsuid_core/utils/database/base_models.py"
DIGEST = "b8f734e4c17f67a58f7ca02ac6ff9d3a8eba6b886701905303e74be0211daba2"
ACCOUNT_PATH = "gsuid_core/utils/database/models.py"
ACCOUNT_DIGEST = "83a2f078e6373106b62519c48b958e937d13dc956a42f08405b088dbcecbcb4a"
CLASSES = ("BaseIDModel", "BaseBotIDModel", "BaseModel", "Bind", "User", "Cache")
PRELUDE = '''"""Native SQLModel classes from Genshin-bots/gsuid_core, commit
96cd7353be3100ff982bccc33e554961837c778e. Source gsuid_core/utils/database/base_models.py.
GPL-3.0, see licenses/gsuid_core-COPYING.txt. Session and driver supplied by Loader.
"""
from typing import Any, Dict, List, Optional, Sequence, Type, TypeVar
from sqlmodel import SQLModel, Field, col, and_, delete, select, update
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import InstrumentedAttribute
from sqlalchemy.sql.expression import func, null, true
from plugin_orm import with_read_session, with_session, logger
from gsuid_core.i18n import t as i18n_t

T = TypeVar("T")
_ModelT = TypeVar("_ModelT", bound=SQLModel)
T_BaseModel = TypeVar("T_BaseModel", bound="BaseModel")
T_BaseIDModel = TypeVar("T_BaseIDModel", bound="BaseIDModel")
T_User = TypeVar("T_User", bound="User")
T_Bind = TypeVar("T_Bind", bound="Bind")
T_Cache = TypeVar("T_Cache", bound="Cache")
_UPSERT_CHUNK = 400
_db_type = "sqlite"
'''


def _verified_source(reference, path, digest):
    if reference is not None and Path(reference).is_dir():
        raw = (Path(reference) / path).read_bytes()
    else:
        with urlopen(BASE + path, timeout=30) as stream:
            raw = stream.read(256 * 1024)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("native SQLModel declarations checksum mismatch")
    return raw.decode("utf-8")


def verified_orm_models(reference=None):
    source = _verified_source(reference, PATH, DIGEST)
    names = ("_mapped_column", *CLASSES)
    nodes = {
        node.name: node
        for node in ast.parse(source).body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef))
    }
    body = (
        PRELUDE
        + "\n\n"
        + "\n\n".join(ast.get_source_segment(source, nodes[name]) for name in names)
        + "\n"
    )
    table = symtable.symtable(body, PATH, "exec")
    defined = {
        s.get_name() for s in table.get_symbols() if s.is_imported() or s.is_assigned()
    }
    todo = [table]
    while todo:
        scope = todo.pop()
        for symbol in scope.get_symbols():
            key = symbol.get_name()
            if (
                symbol.is_global()
                and symbol.is_referenced()
                and key not in defined
                and not hasattr(builtins, key)
            ):
                raise ValueError(
                    f"missing dependency for native SQLModel declarations: {key}"
                )
        todo.extend(scope.get_children())
    accounts = _verified_source(reference, ACCOUNT_PATH, ACCOUNT_DIGEST)
    declarations = [
        ast.get_source_segment(accounts, cls)
        for cls in ast.parse(accounts).body
        if isinstance(cls, ast.ClassDef) and cls.name in ("GsUser", "GsUID", "GsCache")
    ]
    if len(declarations) != 3:
        raise ValueError("missing native account declarations")
    # Reuse the native field definitions, without mapping another SQL table.
    binding = next(
        node
        for node in ast.parse(accounts).body
        if isinstance(node, ast.ClassDef) and node.name == "GsBind"
    )
    fields = [
        ast.get_source_segment(accounts, node)
        for node in binding.body
        if isinstance(node, ast.AnnAssign)
    ]
    bind_fields = "class GsBindFields(Bind):\n" + "".join(
        "    " + field + "\n" for field in fields
    )
    account_models = (
        '"""Pinned gsuid_core account declarations. GPL-3.0, see licenses/gsuid_core-COPYING.txt."""\n'
        "from typing import Optional\nfrom sqlmodel import Field, select, update, or_\n"
        "from sqlalchemy.ext.asyncio import AsyncSession\n"
        "from plugin_orm import with_session\nfrom plugin_orm_models import BaseIDModel, Bind, Cache, User\n\n"
        + bind_fields
        + "\n\n"
        + "\n\n".join(declarations)
        + "\n"
    )
    return {
        "plugin_orm_models.py": body.encode("utf-8"),
        "plugin_account_models.py": account_models.encode("utf-8"),
    }
