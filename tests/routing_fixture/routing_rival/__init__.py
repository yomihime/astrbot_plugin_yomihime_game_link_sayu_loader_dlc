from gsuid_core.sv import Plugins, SV

plugin = Plugins("routing_rival", priority=100)
service = SV("rival", priority=3)


@service.on_fullmatch("inter", prefix=False, block=True)
async def inter(bot, event):
    await bot.send("rival")
