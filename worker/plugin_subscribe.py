"""Profile-local subscription management backed by the worker's owner-scoped ORM."""

from sqlalchemy import update

from port_errors import Unsupported
from plugin_orm import with_session


def _subscriber_key(kind, event):
    if kind == "session" and event.user_type == "group":
        return {"group_id": event.group_id}
    return {"user_id": event.user_id}


class SubscriptionManager:
    def __init__(self, model, orm):
        self.model = model
        self.orm = orm

    def _owner(self, *, event=None, user_id=None, bot_id=None, write=False):
        access = self.orm.access(read_only=not write)
        access.check()
        if access.startup or (write and not access.writable):
            raise Unsupported("subscription operation requires a private owner grant")
        if event is not None:
            user_id, bot_id = event.user_id, event.bot_id
        if (user_id is not None and access.origin.actor != user_id) or (
            bot_id is not None and access.origin.adapter != bot_id
        ):
            raise Unsupported(
                "subscription owner does not match the current invocation"
            )

    @with_session
    async def add_subscribe(
        self,
        session,
        subscribe_type,
        task_name,
        event,
        extra_message=None,
        uid=None,
        extra_data=None,
    ):
        self._owner(event=event, write=True)
        identity = _subscriber_key(subscribe_type, event)
        if "group_id" in identity:
            identity["user_type"] = event.user_type
        identity.update(
            bot_id=event.bot_id, WS_BOT_ID=event.WS_BOT_ID, task_name=task_name, uid=uid
        )
        values = {
            name: getattr(event, name)
            for name in (
                "user_id",
                "bot_id",
                "group_id",
                "bot_self_id",
                "user_type",
                "WS_BOT_ID",
                "msg_id",
            )
        }
        values.update(
            task_name=task_name,
            uid=uid,
            extra_message=extra_message,
            extra_data=extra_data,
        )
        # Acquire SQLite's writer reservation before deciding between insert and
        # update. Concurrent checkouts for the same owner cannot both insert.
        result = await session.execute(
            update(self.model).filter_by(**identity).values(**values)
        )
        if not result.rowcount:
            session.add(self.model(**values))

    async def get_subscribe(
        self,
        task_name,
        user_id=None,
        bot_id=None,
        user_type=None,
        uid=None,
        WS_BOT_ID=None,
    ):
        self._owner(user_id=user_id, bot_id=bot_id)
        where = {"task_name": task_name}
        if user_id and bot_id and user_type:
            where.update(user_id=user_id, bot_id=bot_id, user_type=user_type)
        if uid:
            where["uid"] = uid
        if WS_BOT_ID:
            where["WS_BOT_ID"] = WS_BOT_ID
        return await self.model.select_rows(distinct=False, **where)

    async def delete_subscribe(
        self, subscribe_type, task_name, event, uid=None, WS_BOT_ID=None
    ):
        self._owner(event=event, write=True)
        where = _subscriber_key(subscribe_type, event)
        if uid:
            where["uid"] = uid
        if WS_BOT_ID:
            where["WS_BOT_ID"] = WS_BOT_ID
        await self.model.delete_row(task_name=task_name, **where)

    async def _update(self, subscribe_type, task_name, event, uid, values):
        self._owner(event=event, write=True)
        where = _subscriber_key(subscribe_type, event)
        for key in ("bot_id", "bot_self_id", "user_type", "WS_BOT_ID"):
            where[key] = getattr(event, key)
        if uid:
            where["uid"] = uid
        where["task_name"] = task_name
        await self.model.update_data_by_data(where, values)

    async def update_subscribe_message(
        self, subscribe_type, task_name, event, extra_message, uid=None
    ):
        await self._update(
            subscribe_type, task_name, event, uid, {"extra_message": extra_message}
        )

    async def update_subscribe_data(
        self, subscribe_type, task_name, event, extra_data, uid=None
    ):
        await self._update(
            subscribe_type, task_name, event, uid, {"extra_data": extra_data}
        )

    async def _to_dict(self, data):
        result = {}
        for item in data:
            result.setdefault(str(item.uid), []).append(item)
        return result

    async def muti_task(self, datas, func, attr="uid"):
        from sayu_compat import Message

        private, groups = {}, {}
        for row in datas:
            self._owner(event=row)
            value = getattr(row, attr)
            if not value:
                continue
            message = await func(value)
            if row.user_type == "group":
                key = f"{row.WS_BOT_ID}_{row.group_id}"
                item = groups.setdefault(
                    key, {"success": 0, "fail": 0, "event": row, "push_message": []}
                )
                if "失败" in message:
                    item["fail"] += 1
                    item["push_message"].extend(
                        [
                            Message("text", "\n"),
                            Message("at", row.user_id),
                            Message("text", "\n"),
                            Message("text", message),
                        ]
                    )
                else:
                    item["success"] += 1
            else:
                key = f"{row.WS_BOT_ID}_{row.user_id}"
                private.setdefault(key, {"im": [], "event": row, "push_message": []})[
                    "im"
                ].append(message)
        return private, groups


def install_subscribe(package, orm, *, target=None):
    """Install after ORM initialization. The host supplies no proactive transport."""
    from gsuid_core.utils.database.models import Subscribe

    class GsCoreSubscribe(SubscriptionManager):
        def __init__(self):
            super().__init__(Subscribe, orm)

    manager = GsCoreSubscribe()

    async def send(
        row,
        reply=None,
        option_list=None,
        unsuported_platform=False,
        sep="\n",
        command_tips="请输入以下命令之一:",
        command_start_text="",
        force_direct=False,
        extra_metadata=None,
    ):
        if target is None:
            raise Unsupported("subscription delivery requires a bound target")
        manager._owner(event=row)
        if option_list or extra_metadata or unsuported_platform or reply is None:
            raise Unsupported("subscription delivery options not supported by target")
        kind = "direct" if force_direct else row.user_type
        await target.send(kind, row.group_id if kind == "group" else row.user_id, reply)
        return 0

    Subscribe.send = send
    module = package("gsuid_core.subscribe")
    module.Subscribe = Subscribe
    module.GsCoreSubscribe = GsCoreSubscribe
    module.gs_subscribe = manager
    return manager
