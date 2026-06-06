"""
Character cog — handles !import and !sheet commands.
"""
from __future__ import annotations
import discord
from discord.ext import commands
from discord import app_commands

from utils.parser import parse_compcon_json
from utils.embeds import (
    build_pilot_embed,
    build_mech_embed,
    build_summary_embed,
    build_weapons_embed,
    build_systems_embed,
    build_talents_embed,
)
import utils.storage as storage

MAX_ATTACHMENT_SIZE = 1 * 1024 * 1024  # 1 MB guard


class CharacterCog(commands.Cog, name="Character"):
    """Commands for importing and viewing Lancer pilot sheets."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── !import ───────────────────────────────────────────────────────────────

    @commands.command(name="import", aliases=["i"])
    async def import_char(self, ctx: commands.Context):
        """
        Import a comp/con pilot export.

        Attach your JSON file exported from comp/con (Export → Save Pilot)
        and run this command.

        Example:
            !import  (with the JSON attached)
        """
        if not ctx.message.attachments:
            await ctx.reply(
                "❌ No file attached.\n"
                "Export your pilot from **comp/con** (`Export → Save Pilot`), "
                "then run `!import` with the JSON file attached.",
                mention_author=False,
            )
            return

        attachment = ctx.message.attachments[0]

        # Basic safety checks
        if not attachment.filename.endswith(".json"):
            await ctx.reply(
                "❌ Please attach a `.json` file exported from comp/con.",
                mention_author=False,
            )
            return

        if attachment.size > MAX_ATTACHMENT_SIZE:
            await ctx.reply(
                f"❌ File is too large ({attachment.size // 1024} KB). "
                "Maximum is 1 MB.",
                mention_author=False,
            )
            return

        async with ctx.typing():
            raw = await attachment.read()
            try:
                char = parse_compcon_json(raw)
            except ValueError as e:
                await ctx.reply(f"❌ Could not parse your export:\n```\n{e}\n```", mention_author=False)
                return

            storage.save(ctx.guild.id, ctx.author.id, char, raw.decode("utf-8"))

        embed = build_summary_embed(char)
        embed.set_author(name=f"✅ Imported — {char.pilot.callsign}", icon_url=ctx.author.display_avatar.url)
        await ctx.reply(embed=embed, mention_author=False)

    # ── !sheet ────────────────────────────────────────────────────────────────

    @commands.group(name="sheet", aliases=["s"], invoke_without_command=True)
    async def sheet(self, ctx: commands.Context):
        """
        Display your character sheet.
        Subcommands: pilot, mech, weapons, systems, talents

        Examples:
            !sheet          — summary overview
            !sheet pilot    — pilot stats & skills
            !sheet mech     — active mech stats
            !sheet weapons  — weapon details
            !sheet systems  — system details
            !sheet talents  — talent descriptions
        """
        char = storage.load(ctx.guild.id, ctx.author.id)
        if not char:
            await ctx.reply(
                "❌ No character imported. Use `!import` with your comp/con JSON attached.",
                mention_author=False,
            )
            return
        embed = build_summary_embed(char)
        embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
        await ctx.reply(embed=embed, mention_author=False)

    @sheet.command(name="pilot", aliases=["p"])
    async def sheet_pilot(self, ctx: commands.Context):
        """Display full pilot stats, skills, talents, and licenses."""
        char = storage.load(ctx.guild.id, ctx.author.id)
        if not char:
            await ctx.reply("❌ No character imported. Use `!import` first.", mention_author=False)
            return
        embed = build_pilot_embed(char)
        embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
        await ctx.reply(embed=embed, mention_author=False)

    @sheet.command(name="mech", aliases=["m"])
    async def sheet_mech(self, ctx: commands.Context):
        """Display your active mech's stats."""
        char = storage.load(ctx.guild.id, ctx.author.id)
        if not char:
            await ctx.reply("❌ No character imported. Use `!import` first.", mention_author=False)
            return
        mech = char.active_mech
        if not mech:
            await ctx.reply("❌ No mechs found on this pilot.", mention_author=False)
            return
        embed = build_mech_embed(char, mech)
        embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
        await ctx.reply(embed=embed, mention_author=False)

    @sheet.command(name="weapons", aliases=["w", "weapon"])
    async def sheet_weapons(self, ctx: commands.Context):
        """Display your active mech's weapons in detail."""
        char = storage.load(ctx.guild.id, ctx.author.id)
        if not char:
            await ctx.reply("❌ No character imported. Use `!import` first.", mention_author=False)
            return
        mech = char.active_mech
        if not mech:
            await ctx.reply("❌ No mechs found on this pilot.", mention_author=False)
            return
        embed = build_weapons_embed(char, mech)
        embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
        await ctx.reply(embed=embed, mention_author=False)

    @sheet.command(name="systems", aliases=["sys"])
    async def sheet_systems(self, ctx: commands.Context):
        """Display your active mech's installed systems."""
        char = storage.load(ctx.guild.id, ctx.author.id)
        if not char:
            await ctx.reply("❌ No character imported. Use `!import` first.", mention_author=False)
            return
        mech = char.active_mech
        if not mech:
            await ctx.reply("❌ No mechs found on this pilot.", mention_author=False)
            return
        embed = build_systems_embed(char, mech)
        embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
        await ctx.reply(embed=embed, mention_author=False)

    @sheet.command(name="talents", aliases=["t", "talent"])
    async def sheet_talents(self, ctx: commands.Context):
        """Display your pilot's talents."""
        char = storage.load(ctx.guild.id, ctx.author.id)
        if not char:
            await ctx.reply("❌ No character imported. Use `!import` first.", mention_author=False)
            return
        embed = build_talents_embed(char)
        embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
        await ctx.reply(embed=embed, mention_author=False)

    # ── !delete ───────────────────────────────────────────────────────────────

    @commands.command(name="delete", aliases=["remove"])
    async def delete_char(self, ctx: commands.Context):
        """Remove your imported character from this server."""
        if storage.delete(ctx.guild.id, ctx.author.id):
            await ctx.reply("🗑️ Your character has been removed.", mention_author=False)
        else:
            await ctx.reply("❌ No character found to delete.", mention_author=False)

    # ── !lancer ───────────────────────────────────────────────────────────────

    @commands.command(name="lancer")
    async def lancer_help(self, ctx: commands.Context):
        """Show a quick-start guide for the bot."""
        embed = discord.Embed(
            title="🤖 Lancer Bot — Quick Start",
            description=(
                "Import your **comp/con** pilot export and view your sheet in Discord."
            ),
            color=0xCF2020,
        )
        embed.add_field(
            name="📥 Importing",
            value=(
                "1. In **comp/con**, go to your pilot → **Export → Save Pilot**\n"
                "2. Attach the `.json` file to a Discord message\n"
                "3. Run `!import`"
            ),
            inline=False,
        )
        embed.add_field(
            name="📋 Viewing your sheet",
            value=(
                "`!sheet`          — overview (pilot + mech)\n"
                "`!sheet pilot`    — pilot stats, skills, talents, licenses\n"
                "`!sheet mech`     — active mech stats & HP tracking\n"
                "`!sheet weapons`  — weapon details\n"
                "`!sheet systems`  — installed systems\n"
                "`!sheet talents`  — talent descriptions"
            ),
            inline=False,
        )
        embed.add_field(
            name="🗑️ Removing",
            value="`!delete` — remove your character from this server",
            inline=False,
        )
        embed.set_footer(text="More features coming soon!")
        await ctx.reply(embed=embed, mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(CharacterCog(bot))
