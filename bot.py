"""
Lancer TTRPG Discord Bot
Main entry point - handles character import from comp/con exports
"""
import discord
from discord.ext import commands
import os

# Bot setup with required intents
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"✅ {bot.user} is online!")
    print(f"   Guilds: {len(bot.guilds)}")
    await bot.tree.sync()


# Load cogs
async def load_extensions():
    await bot.load_extension("cogs.character")


@bot.event
async def setup_hook():
    await load_extensions()


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN environment variable not set.\n"
            "Set it in a .env file or export it before running."
        )
    bot.run(token)
