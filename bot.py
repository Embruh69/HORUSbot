"""
Lancer TTRPG Discord Bot
Main entry point — loads config from .env, connects to Discord.
"""
import os
import discord
from discord.ext import commands
from dotenv import load_dotenv

# Load .env before anything else
load_dotenv()

# ── Config from environment ───────────────────────────────────────────────────

DISCORD_TOKEN  = os.getenv("DISCORD_TOKEN")
COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "!")

if not DISCORD_TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN is not set.\n"
        "Copy .env.example to .env and fill in your bot token.\n"
        "Get one at https://discord.com/developers/applications"
    )

# ── Bot setup ─────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True   # required for prefix commands

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)


@bot.event
async def on_ready():
    print(f"✅  Logged in as {bot.user}  (id: {bot.user.id})")
    print(f"    Prefix : {COMMAND_PREFIX}")
    print(f"    Guilds : {len(bot.guilds)}")
    await bot.tree.sync()


@bot.event
async def setup_hook():
    await bot.load_extension("cogs.character")


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN, log_handler=None)  # discord.py sets up logging itself
