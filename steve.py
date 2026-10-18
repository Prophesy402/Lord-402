"""
Pyro Steve: a second Discord bot account that runs the Fireball game, so Lord 402
has nothing to do with it. Only starts when the FIREBALL_TOKEN variable is set
(see bot.py). Runs inside the same Railway service as Lord 402.
"""
import logging

import discord
from discord.ext import commands

log = logging.getLogger("steve")


async def run(token: str) -> None:
    try:
        await _run(token, read_messages=True)    # needed for typed "X2" satellite calls
    except discord.PrivilegedIntentsRequired:
        log.warning("Pyro Steve: turn on Message Content Intent in the Developer Portal for typed "
                    "satellite calls; running with buttons only")
        await _run(token, read_messages=False)


async def _run(token: str, read_messages: bool) -> None:
    intents = discord.Intents.default()
    intents.message_content = read_messages
    steve = commands.Bot(command_prefix="!", intents=intents)
    # The bot itself is Pyro Steve, so no webhook trick is needed. This goes on the bot, not the
    # fireball module: load_extension runs a fresh copy of fireball.py, so a module flag set here is lost.
    steve.is_pyro_steve = True

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
    except discord.PrivilegedIntentsRequired:
        if read_messages:
            raise
        log.exception("Pyro Steve stopped; Lord 402 and duck races are unaffected")
    except Exception:
        log.exception("Pyro Steve stopped; Lord 402 and duck races are unaffected")
