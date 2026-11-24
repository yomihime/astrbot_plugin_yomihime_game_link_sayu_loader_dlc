from gsuid_core.sv import Plugins, SV

plugin = Plugins(
    "routing_probe", prefix=["old"], force_prefix=["forced"], alias=["DeclaredAlias"]
)
a = SV("a", priority=9)
b = SV("b", priority=4)


@a.on_fullmatch("early", aliases=["tool_alias"])
async def early(bot, event):
    await bot.send("early")


@a.on_fullmatch("race", block=True)
async def a_race(bot, event):
    await bot.send("A")


@a.on_fullmatch("inter", block=True, prefix=False)
async def inter(bot, event):
    await bot.send("A")


@b.on_fullmatch("race", block=True)
async def b_race(bot, event):
    await bot.send("B")


@a.on_fullmatch("bare", prefix=False)
async def bare(bot, event):
    await bot.send("bare")


def late(word):
    @a.on_fullmatch(word, aliases=["tool_alias"])
    async def show(bot, event):
        await bot.send(word)


def extra_service():
    return SV("additional")
