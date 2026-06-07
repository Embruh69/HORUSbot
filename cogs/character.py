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


    # ── !hp ───────────────────────────────────────────────────────────────────

    @commands.command(name="hp")
    async def set_hp(self, ctx: commands.Context, amount: str = None):
        """
        Adjust or set your mech HP.
        !hp        — show current HP
        !hp +5     — add 5 HP
        !hp -3     — subtract 3 HP (triggers structure check if HP hits 0)
        !hp 10     — set HP to exactly 10
        """
        import json as _json
        from utils.lancer_checks import roll_structure_check, attach_cascade

        char = storage.load(ctx.guild.id, ctx.author.id)
        if not char:
            await ctx.reply("No character imported. Use `!import` first.", mention_author=False)
            return
        mech = char.active_mech
        if not mech:
            await ctx.reply("No active mech found.", mention_author=False)
            return
        ms = mech.stats

        if amount is None:
            filled = ms.current_hp
            bar = "\u2588" * filled + "\u2591" * (ms.hp - filled)
            await ctx.reply(
                f"\u2764\ufe0f **{mech.name}** HP: `{bar}` **{ms.current_hp}/{ms.hp}**",
                mention_author=False,
            )
            return

        amount = amount.strip()
        is_relative = amount.startswith(("+", "-"))
        try:
            delta = int(amount)
        except ValueError:
            await ctx.reply("Usage: `!hp +5`, `!hp -3`, or `!hp 10`", mention_author=False)
            return

        raw = storage.load_raw(ctx.guild.id, ctx.author.id)
        data = _json.loads(raw)
        cur = data["data"]["mechs"][0]["stats"]["current"]
        old_hp = cur.get("hp", ms.hp)
        new_hp = (old_hp + delta) if is_relative else delta

        structure_result = None
        if new_hp <= 0 and cur.get("structure", ms.structure) > 0:
            overflow = abs(new_hp)
            structure_result = roll_structure_check(
                structure_before=cur.get("structure", ms.structure),
                max_structure=ms.structure,
                hp_overflow=overflow,
            )
            attach_cascade(structure_result, mech)
            cur["structure"] = max(0, structure_result.structure_after)
            # HP resets to max on structure loss, then overflow comes off that
            cur["hp"] = max(0, ms.hp - overflow) if structure_result.structure_after > 0 else 0
        else:
            cur["hp"] = max(0, min(ms.hp, new_hp))

        storage.save_raw(ctx.guild.id, ctx.author.id, char.pilot.callsign, _json.dumps(data))

        final_hp = cur["hp"]
        bar = "\u2588" * final_hp + "\u2591" * (ms.hp - final_hp)
        change = f"({delta:+})" if is_relative else "(set)"
        await ctx.reply(
            f"\u2764\ufe0f **{mech.name}** HP: `{bar}` **{old_hp}** \u2192 **{final_hp}/{ms.hp}** {change}",
            mention_author=False,
        )
        if structure_result:
            from cogs.use import _build_structure_embed
            await ctx.send(embed=_build_structure_embed(structure_result, mech))

    # ── !heat ─────────────────────────────────────────────────────────────────

    @commands.command(name="heat")
    async def set_heat(self, ctx: commands.Context, amount: str = None):
        """
        Adjust or set your mech Heat.
        !heat       — show current heat
        !heat +4    — add 4 heat (triggers stress check if over heatcap)
        !heat -2    — reduce heat by 2
        !heat 0     — set heat to exactly 0
        """
        import json as _json
        from utils.lancer_checks import roll_stress_check, attach_cascade

        char = storage.load(ctx.guild.id, ctx.author.id)
        if not char:
            await ctx.reply("No character imported. Use `!import` first.", mention_author=False)
            return
        mech = char.active_mech
        if not mech:
            await ctx.reply("No active mech found.", mention_author=False)
            return
        ms = mech.stats

        if amount is None:
            filled = ms.current_heat
            bar = "\u2588" * filled + "\u2591" * (ms.heatcap - filled)
            dz = "  \U0001f321\ufe0f DANGER ZONE" if filled >= ms.heatcap // 2 else ""
            await ctx.reply(
                f"\U0001f321\ufe0f **{mech.name}** Heat: `{bar}` **{ms.current_heat}/{ms.heatcap}**{dz}",
                mention_author=False,
            )
            return

        amount = amount.strip()
        is_relative = amount.startswith(("+", "-"))
        try:
            delta = int(amount)
        except ValueError:
            await ctx.reply("Usage: `!heat +4`, `!heat -2`, or `!heat 0`", mention_author=False)
            return

        raw = storage.load_raw(ctx.guild.id, ctx.author.id)
        data = _json.loads(raw)
        cur = data["data"]["mechs"][0]["stats"]["current"]
        old_heat = cur.get("heat", 0)
        new_heat_raw = (old_heat + delta) if is_relative else delta

        stress_result = None
        if new_heat_raw > ms.heatcap and cur.get("stress", ms.stress) > 0:
            overflow = new_heat_raw - ms.heatcap
            stress_result = roll_stress_check(
                stress_before=cur.get("stress", ms.stress),
                max_stress=ms.stress,
                heat_overflow=overflow,
            )
            attach_cascade(stress_result, mech)
            cur["stress"] = max(0, stress_result.stress_after)
            cur["heat"] = overflow
        else:
            cur["heat"] = max(0, min(ms.heatcap, new_heat_raw))

        storage.save_raw(ctx.guild.id, ctx.author.id, char.pilot.callsign, _json.dumps(data))

        final_heat = cur["heat"]
        bar = "\u2588" * min(final_heat, ms.heatcap) + "\u2591" * max(0, ms.heatcap - final_heat)
        change = f"({delta:+})" if is_relative else "(set)"
        dz = "  \U0001f321\ufe0f **DANGER ZONE!**" if final_heat >= ms.heatcap // 2 and not stress_result else ""
        await ctx.reply(
            f"\U0001f321\ufe0f **{mech.name}** Heat: `{bar}` **{old_heat}** \u2192 **{final_heat}/{ms.heatcap}** {change}{dz}",
            mention_author=False,
        )
        if stress_result:
            from cogs.use import _build_stress_embed
            await ctx.send(embed=_build_stress_embed(stress_result, mech))

    # ── !fr / !fullrepair ─────────────────────────────────────────────────────

    @commands.command(name="fr", aliases=["fullrepair", "full_repair"])
    async def full_repair(self, ctx: commands.Context):
        """
        Perform a Full Repair on your active mech.
        Resets HP, Heat, Structure, Stress, and Burn to max/zero.
        Clears statuses and reloads LIMITED systems.
        Usage: !fr
        """
        import json as _json

        char = storage.load(ctx.guild.id, ctx.author.id)
        if not char:
            await ctx.reply("No character imported. Use `!import` first.", mention_author=False)
            return
        mech = char.active_mech
        if not mech:
            await ctx.reply("No active mech found.", mention_author=False)
            return
        ms = mech.stats

        raw = storage.load_raw(ctx.guild.id, ctx.author.id)
        data = _json.loads(raw)
        mech_data = data["data"]["mechs"][0]
        cur = mech_data["stats"]["current"]

        old = {
            "hp":        cur.get("hp", ms.hp),
            "heat":      cur.get("heat", 0),
            "structure": cur.get("structure", ms.structure),
            "stress":    cur.get("stress", ms.stress),
            "burn":      cur.get("burn", 0),
        }

        cur["hp"]        = ms.hp
        cur["heat"]      = 0
        cur["structure"] = ms.structure
        cur["stress"]    = ms.stress
        cur["burn"]      = 0

        for key in ("statuses", "conditions"):
            if key in mech_data:
                mech_data[key] = []

        active_loadout_idx = mech_data.get("active_loadout_index", 0)
        loadouts = mech_data.get("loadouts", [])
        if loadouts:
            lo = loadouts[min(active_loadout_idx, len(loadouts) - 1)]
            for sys_slot in lo.get("systems", []):
                if "uses" in sys_slot:
                    sys_slot["uses"] = sys_slot.get("max_uses", sys_slot["uses"])
            for mount in lo.get("mounts", []):
                for slot in mount.get("slots", []):
                    w = slot.get("weapon")
                    if w and "uses" in w:
                        w["uses"] = w.get("max_uses", w["uses"])

        storage.save_raw(ctx.guild.id, ctx.author.id, char.pilot.callsign, _json.dumps(data))

        hp_bar    = "\u2588" * ms.hp
        heat_bar  = "\u2591" * ms.heatcap
        str_bar   = "\u2588" * ms.structure
        stress_bar = "\u2588" * ms.stress

        embed = discord.Embed(
            title=f"\U0001f527 Full Repair \u2014 {mech.name}",
            description=(
                f"*{char.pilot.callsign}* has performed a Full Repair. All systems restored."
            ),
            color=0x4CAF50,
        )
        embed.add_field(
            name="\u2764\ufe0f HP",
            value=f"`{hp_bar}` {old['hp']} \u2192 **{ms.hp}/{ms.hp}**",
            inline=True,
        )
        embed.add_field(
            name="\U0001f321\ufe0f Heat",
            value=f"`{heat_bar}` {old['heat']} \u2192 **0/{ms.heatcap}**",
            inline=True,
        )
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        embed.add_field(
            name="\U0001f6e1\ufe0f Structure",
            value=f"`{str_bar}` {old['structure']} \u2192 **{ms.structure}/{ms.structure}**",
            inline=True,
        )
        embed.add_field(
            name="\u26db\ufe0f Stress",
            value=f"`{stress_bar}` {old['stress']} \u2192 **{ms.stress}/{ms.stress}**",
            inline=True,
        )
        if old["burn"] > 0:
            embed.add_field(name="\U0001f525 Burn", value=f"{old['burn']} \u2192 **0** (cleared)", inline=True)
        embed.set_footer(text="LIMITED systems reloaded \u00b7 Statuses cleared")
        embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
        await ctx.reply(embed=embed, mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(CharacterCog(bot))
