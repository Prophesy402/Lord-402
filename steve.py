"""
Pyro Steve: a second Discord bot account that runs the Fireball game, so Lord 402
has nothing to do with it. Only starts when the FIREBALL_TOKEN variable is set
(see bot.py). Runs inside the same Railway service as Lord 402.
"""
import logging

import discord
from discord.ext import commands

import fireball

log = logging.getLogger("steve")


async def run(token: str) -> None:
    fireball.USE_WEBHOOK = False           # the bot itself is Pyro Steve, no webhook trick needed
    intents = discord.Intents.default()
    steve = commands.Bot(command_prefix="!", intents=intents)

    @steve.event
    async def setup_hook():
        await steve.load_extension("fireball")

    @steve.event
    async def on_ready():
        try:
            synced = await steve.tree.sync()
            log.info("Pyro Steve online as %s; synced %d slash command(s)", steve.user, len(synced))
        except Exception:
            log.exception("Pyro Steve could not sync slash commands")

    try:
        async with steve:
            await steve.start(token)
    except Exception:
        log.exception("Pyro Steve stopped; Lord 402 and duck races are unaffected")
