"""Ordinary Sayu plugin used unchanged on both sides of a differential run."""

import json

from gsuid_core.aps import scheduler
from gsuid_core.bot import Bot
from gsuid_core.data_store import get_res_path
from gsuid_core.models import Event
from gsuid_core.server import on_core_shutdown, on_core_start
from gsuid_core.sv import Plugins, SV

Plugins(name="contract_probe", force_prefix=["cp"], allow_empty_prefix=False)
early = SV("probe-early", priority=2)
middle = SV("probe-middle", priority=4)
late = SV("probe-late", priority=6)
administration = SV("probe-admin", pm=0)
groups = SV("probe-group", area="GROUP")
direct = SV("probe-direct", area="DIRECT")
root = get_res_path("contract_probe")
calls = 0


def trace(label, **fields):
    with (root / "trace.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"label": label, **fields}, ensure_ascii=False) + "\n")


trace("import")


@on_core_start
async def startup():
    trace("start")


@on_core_shutdown
async def shutdown():
    trace("shutdown")


@scheduler.scheduled_job("interval", seconds=0.25, id="probe-clock")
async def clock():
    trace("tick")


@middle.on_fullmatch("state")
async def state(bot: Bot, ev: Event):
    global calls
    calls += 1
    path = root / "count.txt"
    total = int(path.read_text()) + 1 if path.exists() else 1
    path.write_text(str(total))
    trace("state", calls=calls, total=total)
    await bot.send(f"count {calls}/{total}")


@middle.on_command("inspect")
async def inspect_event(bot: Bot, ev: Event):
    fields = {
        key: getattr(ev, key)
        for key in (
            "raw_text",
            "text",
            "command",
            "user_id",
            "group_id",
            "user_type",
            "msg_id",
            "bot_id",
        )
    }
    trace("inspect", **fields)
    await bot.send(json.dumps(fields, sort_keys=True))


@early.on_fullmatch("order", block=False)
async def first(bot: Bot, ev: Event):
    trace("first")
    await bot.send("first")


@middle.on_fullmatch("order", block=True)
async def second(bot: Bot, ev: Event):
    trace("second")
    await bot.send("second")


@late.on_fullmatch("order")
async def forbidden(bot: Bot, ev: Event):
    trace("forbidden")
    await bot.send("forbidden")


@middle.on_fullmatch("fail")
async def failure(bot: Bot, ev: Event):
    trace("failure")
    raise ValueError("fixture controlled failure")


@administration.on_fullmatch("admin")
async def admin_only(bot: Bot, ev: Event):
    trace("admin", actor=ev.user_id, pm=ev.user_pm)
    await bot.send(f"admin {ev.user_id} {ev.user_pm}")


@groups.on_fullmatch("group")
async def group_only(bot: Bot, ev: Event):
    trace("group", actor=ev.user_id, room=ev.group_id)
    await bot.send(f"group {ev.user_id} {ev.group_id}")


@direct.on_fullmatch("direct")
async def direct_only(bot: Bot, ev: Event):
    trace("direct", actor=ev.user_id)
    await bot.send(f"direct {ev.user_id}")
