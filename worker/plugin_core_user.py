"""CoreUser language and profile rows, scoped to the private owner's SQLite file.

SQLModel fields follow gsuid_core/utils/database/models.py at
96cd7353be3100ff982bccc33e554961837c778e, GPL-3.0 (see licenses/gsuid_core-COPYING.txt).
"""

from sqlalchemy import Index, UniqueConstraint
from sqlmodel import Field

from plugin_orm import with_session
from plugin_orm_models import BaseBotIDModel
from plugin_user_language import require_owner


class CoreUser(BaseBotIDModel, table=True):
    __table_args__ = (
        UniqueConstraint("user_id", "group_id", "user_name", name="record_coreuser"),
        Index("idx_user_id", "user_id"),
        Index("idx_coreuser_group_id", "group_id"),
        {"extend_existing": True},
    )

    user_id: str = Field(default=None, title="账号")
    group_id: str | None = Field(default=None, title="群号")
    user_name: str | None = Field(default="1", title="用户名")
    user_icon: str | None = Field(default="1", title="用户头像")
    language: str | None = Field(default=None, title="用户语言")

    @classmethod
    async def get_user_lang(cls, user_id, bot_id=None):
        from gsuid_core.i18n import get_lang, is_supported

        owner = require_owner(user_id, bot_id)
        row = await cls.base_select_data(user_id=user_id, bot_id=owner.adapter)
        return (
            row.language
            if row is not None and is_supported(row.language)
            else get_lang()
        )

    @classmethod
    async def set_user_lang(cls, user_id, bot_id, language):
        require_owner(user_id, bot_id, writable=True)
        # Native updates all existing user/group rows; it does not provision a
        # missing actor. Its successful SQL UPDATE returns 0 even for zero rows.
        return await cls.update_data_by_xx(
            {"user_id": user_id, "bot_id": bot_id}, language=language
        )

    @classmethod
    async def insert_user(cls, bot_id, user_id, group_id, user_name, user_icon):
        require_owner(user_id, bot_id)
        key = dict(bot_id=bot_id, user_id=user_id, group_id=group_id)
        row = await cls.base_select_data(
            **key, user_name=user_name, user_icon=user_icon
        )
        if row is not None:
            return 1
        require_owner(user_id, bot_id, writable=True)
        return await cls._store_user(key, user_name, user_icon)

    @classmethod
    @with_session
    async def _store_user(cls, session, key, user_name, user_icon):
        row = await cls.base_select_data(**key)
        fields = dict(
            user_name=user_name if user_name is not None else "1",
            user_icon=user_icon if user_icon is not None else "1",
        )
        if row is None:
            await cls.full_insert_data(**key, **fields)
        else:
            await cls.update_data_by_xx(key, **fields)
        return 1
