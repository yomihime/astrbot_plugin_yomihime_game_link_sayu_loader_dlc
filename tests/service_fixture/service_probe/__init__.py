"""Identical service declarations and management calls for both runners."""

from gsuid_core.sv import Plugins, SV

plugin = Plugins("service_probe", force_prefix=["sp"], allow_empty_prefix=False)
query = SV("probe-query", enabled=False)
restricted = SV("probe-restricted", white_list=["another"])
moderated = SV(
    "probe-moderated", pm=2, white_list=["room"], black_list=["excluded"], area="GROUP"
)


@moderated.on_fullmatch("act", to_me=True)
async def moderate(bot, ev):
    await bot.send("granted")


@query.on_fullmatch("query")
async def show(bot, ev):
    await bot.send("query")


@restricted.on_fullmatch("restricted")
async def restricted_query(bot, ev):
    await bot.send("restricted")


def activate():
    query.enable()
    restricted.enable()
    query.set(priority=2)


def close_plugin():
    plugin.set(False, enabled=False)


def close_service():
    plugin.set(False, enabled=True)
    query.disable()
    query.set(False, priority=3)
