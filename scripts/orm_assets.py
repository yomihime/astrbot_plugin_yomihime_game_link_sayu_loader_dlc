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
CLASSES = ("BaseIDModel", "BaseBotIDModel", "BaseModel", "Bind", "User")
PRELUDE = '''"""Five native SQLModel classes from Genshin-bots/gsuid_core, commit
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
_UPSERT_CHUNK = 400
_db_type = "sqlite"
'''


def verified_orm_models(reference=None):
    if reference is not None and Path(reference).is_dir():
        raw = (Path(reference) / PATH).read_bytes()
    else:
        with urlopen(BASE + PATH, timeout=30) as stream:
            raw = stream.read(256 * 1024)
    if hashlib.sha256(raw).hexdigest() != DIGEST:
        raise ValueError("native SQLModel declarations checksum mismatch")
    source = raw.decode("utf-8")
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
    return {"plugin_orm_models.py": body.encode("utf-8")}
