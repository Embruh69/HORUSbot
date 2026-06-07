"""
Use cog — !use and commands for weapons and systems.

Features
--------
* Fuzzy, case-insensitive item lookup across weapons AND systems
* Disambiguation prompt when multiple items share a name
* Auto-rolls all dice found in effects/actions (e.g. 1d6+2)
* Full attack roll (1d20 + grit) with accuracy / difficulty bonus dice
* Self-heat display from tg_heat_self tags
* HP / Heat tracking buttons on weapon hit results
"""
from __future__ import annotations
import re
import discord
from discord.ext import commands
from utils.tags import format_tag

from utils.parser import LancerCharacter, Weapon, System, SystemAction
from utils.dice import (
    roll_expression,
    roll_accuracy,
    roll_attack,
    roll_damage_crit,
    roll_all_dice_in_text,
    DiceResult,
    AttackRollResult,
    AccuracyResult,
)
import utils.storage as storage
from utils.lancer_checks import (
    roll_structure_check,
    roll_stress_check,
    attach_cascade,
    _nhp_names,
    StructureResult,
    StressResult,
)

# ── colour constants ──────────────────────────────────────────────────────────
DAMAGE_COLORS = {
    "kinetic":  0x9E9E9E,
    "energy":   0x2196F3,
    "explosive":0xE8871A,
    "burn":     0xFF5722,
    "heat":     0xFF9800,
    "variable": 0x9C27B0,
}
DEFAULT_COLOR = 0xCF2020


def _item_color(item: Weapon | System) -> int:
    if isinstance(item, Weapon) and item.damage:
        dtype = item.damage[0].get("type", "").lower()
        return DAMAGE_COLORS.get(dtype, DEFAULT_COLOR)
    return DEFAULT_COLOR


# ── fuzzy search ──────────────────────────────────────────────────────────────

def _fuzzy_match(query: str, items: list[Weapon | System]) -> list[Weapon | System]:
    """
    Return items whose name contains every word in `query` (case-insensitive).
    Falls back to substring match on the full query string.
    """
    q = query.strip().lower()
    words = q.split()

    # All-word match (best)
    multi = [i for i in items if all(w in i.name.lower() for w in words)]
    if multi:
        return multi

    # Single substring fallback
    return [i for i in items if q in i.name.lower()]


def _all_items(mech) -> list[Weapon | System]:
    return list(mech.weapons) + list(mech.systems)


# ── damage roll helpers ───────────────────────────────────────────────────────

def _roll_damage(damage_list: list[dict]) -> list[tuple[dict, DiceResult]]:
    """Roll each damage entry and return (damage_dict, DiceResult) pairs."""
    results = []
    for d in damage_list:
        val = str(d.get("val", "0"))
        dtype = d.get("type", "?")
        result = roll_expression(val, label=dtype)
        results.append((d, result))
    return results


def _format_damage_rolls(rolled: list[tuple[dict, DiceResult]]) -> str:
    lines = []
    for d, r in rolled:
        ap = " **AP**" if d.get("ap") else ""
        target = " *(self)*" if d.get("target") == "self" else ""
        aoe = " *(AOE)*" if d.get("aoe") else ""
        lines.append(f"**{r.label}**{ap}{target}{aoe}: {r}")
    return "\n".join(lines)


def _total_self_heat(
    item: Weapon | System,
    action: SystemAction | None = None
) -> list[DiceResult]:
    """
    Collect all self-heat sources from tags AND action damage.
    Returns rolled DiceResults.
    """
    heat_results = []

    # From tg_heat_self tag
    hs = item.heat_self()
    if hs is not None:
        heat_results.append(roll_expression(str(hs), label="Self Heat (tag)"))

    # From action damage with target=self and type=Heat
    if action:
        for d in action.damage:
            if d.get("target") == "self" and d.get("type", "").lower() == "heat":
                heat_results.append(
                    roll_expression(str(d.get("val", 0)), label="Self Heat (action)")
                )

    return heat_results


# ── Discord Views (interactive buttons) ──────────────────────────────────────

class HeatHPView(discord.ui.View):
    """
    Buttons to apply HP damage or heat to the stored character.
    Each button updates the DB and edits the embed to show current values.
    """

    def __init__(
        self,
        guild_id: int,
        user_id: int,
        hp_damage: int = 0,
        heat_gain: int = 0,
        self_hp_damage: int = 0,
        self_heat_gain: int = 0,
    ):
        print("HeatHPView init start")
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.user_id = user_id
        self.hp_damage = hp_damage
        self.heat_gain = heat_gain
        self.self_hp_damage = self_hp_damage
        self.self_heat_gain = self_heat_gain

        # Dynamically show/hide buttons based on what's relevant
        if hp_damage <= 0:
            self.apply_hp.disabled = True
            self.apply_hp.label = "No HP damage"
        else:
            self.apply_hp.label = f"Apply {hp_damage} HP dmg to target"

        if heat_gain <= 0:
            self.apply_heat.disabled = True
            self.apply_heat.label = "No Heat"
        else:
            self.apply_heat.label = f"+{heat_gain} Heat to target"

        if self_hp_damage <= 0:
            self.apply_self_hp.disabled = True
            self.apply_self_hp.label = "No self HP"
        else:
            self.apply_self_hp.label = f"Apply {self_hp_damage} self HP dmg"

        if self_heat_gain <= 0:
            self.apply_self_heat.disabled = True
            self.apply_self_heat.label = "No self Heat"
        else:
            self.apply_self_heat.label = f"+{self_heat_gain} self Heat"

    async def _update_and_reply(self, interaction: discord.Interaction, msg: str):
        await interaction.response.send_message(msg, ephemeral=False)

    @discord.ui.button(label="Apply HP dmg", style=discord.ButtonStyle.danger, row=0)
    async def apply_hp(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            f"📊 Track **{self.hp_damage} HP damage** on your target manually "
            f"(target HP tracking coming soon!).",
            ephemeral=True,
        )

    @discord.ui.button(label="+Heat to target", style=discord.ButtonStyle.secondary, row=0)
    async def apply_heat(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            f"🔥 Track **{self.heat_gain} Heat** on your target manually.",
            ephemeral=True,
        )

    @discord.ui.button(label="Apply self HP", style=discord.ButtonStyle.danger, row=1)
    async def apply_self_hp(self, interaction: discord.Interaction, button: discord.ui.Button):
        import json as _json
        char = storage.load(self.guild_id, self.user_id)
        if not char or not char.active_mech:
            await interaction.response.send_message("❌ Character not found.", ephemeral=True)
            return

        raw = storage.load_raw(self.guild_id, self.user_id)
        data = _json.loads(raw)
        mech_data = data["data"]["mechs"][0]
        cur = mech_data["stats"]["current"]
        ms = char.active_mech.stats

        old_hp = cur.get("hp", ms.hp)
        new_hp = old_hp - self.self_hp_damage
        overflow = max(0, -new_hp)    # damage past 0 HP

        structure_result: StructureResult | None = None

        if new_hp <= 0 and ms.current_structure > 0:
            # ── Structure check triggered ──────────────────────────────────
            struct_result = roll_structure_check(
                structure_before=cur.get("structure", ms.structure),
                max_structure=ms.structure,
                hp_overflow=overflow,
            )
            attach_cascade(struct_result, char.active_mech)
            structure_result = struct_result

            # Structure check: reset HP to max then apply overflow damage
            new_structure = max(0, struct_result.structure_after)
            cur["structure"] = new_structure
            # HP resets to max on structure loss, then overflow comes off that
            cur["hp"] = max(0, ms.hp - overflow) if new_structure > 0 else 0
        else:
            cur["hp"] = max(0, new_hp)

        storage.save_raw(self.guild_id, self.user_id, char.pilot.callsign, _json.dumps(data))
        button.disabled = True
        self.stop()

        # ── Acknowledge the interaction first ─────────────────────────────
        await interaction.response.defer()

        # ── HP update message ─────────────────────────────────────────────
        if structure_result:
            hp_msg = (
                f"❤️ HP: **{old_hp}** → **0** (−{self.self_hp_damage})"
                + (f"  · overflow: **{overflow}**" if overflow else "")
            )
        else:
            hp_msg = f"❤️ HP: **{old_hp}** → **{max(0,new_hp)}** (−{self.self_hp_damage})"

        await interaction.followup.send(hp_msg)

        # ── Structure check embed ─────────────────────────────────────────
        if structure_result:
            embed = _build_structure_embed(structure_result, char.active_mech)
            await interaction.followup.send(embed=embed)

    @discord.ui.button(label="+Self Heat", style=discord.ButtonStyle.primary, row=1)
    async def apply_self_heat(self, interaction: discord.Interaction, button: discord.ui.Button):
        import json as _json
        char = storage.load(self.guild_id, self.user_id)
        if not char or not char.active_mech:
            await interaction.response.send_message("❌ Character not found.", ephemeral=True)
            return

        raw = storage.load_raw(self.guild_id, self.user_id)
        data = _json.loads(raw)
        mech_data = data["data"]["mechs"][0]
        cur = mech_data["stats"]["current"]
        ms = char.active_mech.stats

        old_heat = cur.get("heat", 0)
        new_heat_raw = old_heat + self.self_heat_gain
        overflow = max(0, new_heat_raw - ms.heatcap)   # heat past cap

        stress_result: StressResult | None = None

        # Stress triggers when heat EXCEEDS (not just meets) heatcap
        if new_heat_raw > ms.heatcap and cur.get("stress", ms.stress) > 0:
            # ── Stress check triggered ─────────────────────────────────────
            s_result = roll_stress_check(
                stress_before=cur.get("stress", ms.stress),
                max_stress=ms.stress,
                heat_overflow=overflow,
            )
            attach_cascade(s_result, char.active_mech)
            stress_result = s_result

            # Reset heat to 0, store overflow so caller can see it
            new_stress = max(0, s_result.stress_after)
            cur["stress"] = new_stress
            cur["heat"] = overflow   # carry overflow heat into next round
        else:
            cur["heat"] = min(ms.heatcap, new_heat_raw)

        storage.save_raw(self.guild_id, self.user_id, char.pilot.callsign, _json.dumps(data))
        button.disabled = True
        self.stop()

        await interaction.response.defer()

        # ── Heat update message ───────────────────────────────────────────
        final_heat = cur["heat"]
        if stress_result:
            heat_msg = (
                f"🔥 Heat: **{old_heat}** → **{ms.heatcap}** (OVERLOAD!)"
                + (f"  · overflow: **{overflow}** carried forward" if overflow else "")
            )
        else:
            danger_zone = final_heat >= (ms.heatcap // 2)
            dz_str = "  🌡️ **DANGER ZONE!**" if danger_zone else ""
            heat_msg = f"🔥 Heat: **{old_heat}** → **{final_heat}**/{ms.heatcap} (+{self.self_heat_gain}){dz_str}"

        await interaction.followup.send(heat_msg)

        # ── Stress check embed ────────────────────────────────────────────
        if stress_result:
            embed = _build_stress_embed(stress_result, char.active_mech)
            await interaction.followup.send(embed=embed)


# ── Structure / Stress embed builders ────────────────────────────────────────

def _build_structure_embed(result: StructureResult, mech) -> discord.Embed:
    """Build the structure-check result embed."""
    color = 0x1A1A1A if result.destroyed else (0xFF5252 if result.lowest == 1 else 0xFF9800)
    embed = discord.Embed(
        title=f"🦾 Structure Check — {result.result_name}",
        color=color,
    )

    # Dice pool
    if result.dice_rolled:
        dice_str = "  ".join(f"**{d}**" if d == result.lowest else str(d) for d in result.dice_rolled)
        n = len(result.dice_rolled)
        embed.add_field(
            name=f"🎲 Rolled {n}d6 (lowest is worst)",
            value=dice_str + "\n→ Kept: **" + str(result.lowest) + "**",
            inline=False,
        )

    # Structure track
    struct_pips = "█" * result.structure_after + "░" * (result.structure_before - result.structure_after)
    if result.structure_before > 0:
        embed.add_field(
            name="🛡️ Structure",
            value=f"`{struct_pips}` {result.structure_after}/{result.structure_before} → **{result.structure_after}**",
            inline=True,
        )

    if result.hp_overflow:
        embed.add_field(name="💢 HP Overflow", value=f"**{result.hp_overflow}** damage carries to next structure", inline=True)

    # Result detail
    embed.add_field(name="📋 Result", value=result.result_detail, inline=False)

    # NHP cascade
    if result.nhp_present:
        if result.cascade_triggered:
            embed.add_field(
                name="NHP Cascade Check",
                value=f"d20 roll: **{result.cascade_roll}** = 1  →  ⚠️ **CASCADE TRIGGERED!**\nYour NHP begins to cascade. Consult the GM.",
                inline=False,
            )
        else:
            embed.add_field(
                name="NHP Cascade Check",
                value=f"d20 roll: **{result.cascade_roll}** != 1  →  ✅ No cascade.",
                inline=False,
            )

    return embed


def _build_stress_embed(result: StressResult, mech) -> discord.Embed:
    """Build the reactor stress check result embed."""
    color = 0x9C27B0 if result.meltdown else (0xFF5252 if result.lowest == 1 else 0xFF9800)
    embed = discord.Embed(
        title=f"☢️ Reactor Stress Check — {result.result_name}",
        color=color,
    )

    if result.dice_rolled:
        dice_str = "  ".join(f"**{d}**" if d == result.lowest else str(d) for d in result.dice_rolled)
        n = len(result.dice_rolled)
        embed.add_field(
            name=f"🎲 Rolled {n}d6 (lowest is worst)",
            value=dice_str + "\n→ Kept: **" + str(result.lowest) + "**",
            inline=False,
        )

    stress_pips = "█" * result.stress_after + "░" * (result.stress_before - result.stress_after)
    if result.stress_before > 0:
        embed.add_field(
            name="⚛️ Reactor Stress",
            value=f"`{stress_pips}` {result.stress_after}/{result.stress_before} → **{result.stress_after}**",
            inline=True,
        )

    if result.heat_overflow:
        embed.add_field(name="🌡️ Heat Overflow", value=f"**{result.heat_overflow}** heat carried forward", inline=True)

    embed.add_field(name="📋 Result", value=result.result_detail, inline=False)

    if result.nhp_present:
        if result.cascade_triggered:
            embed.add_field(
                name="🤖 NHP Cascade Check",
                value=f"d20 roll: **{result.cascade_roll}** ≤ 10  →  ⚠️ **CASCADE TRIGGERED!**\nYour NHP begins to cascade. Consult the GM.",
                inline=False,
            )
        else:
            embed.add_field(
                name="🤖 NHP Cascade Check",
                value=f"d20 roll: **{result.cascade_roll}** > 10  →  ✅ No cascade.",
                inline=False,
            )

    return embed


class DisambiguateView(discord.ui.View):
    """Shows numbered buttons when multiple items match a query."""

    def __init__(self, items: list[Weapon | System], ctx: commands.Context, accuracy: int, difficulty: int):
        super().__init__(timeout=30)
        self.chosen: Weapon | System | None = None
        self.ctx = ctx
        self.accuracy = accuracy
        self.difficulty = difficulty

        for i, item in enumerate(items[:5]):  # max 5 buttons
            kind = "⚔️" if isinstance(item, Weapon) else "🔧"
            btn = discord.ui.Button(
                label=f"{kind} {item.name}",
                style=discord.ButtonStyle.secondary,
                custom_id=str(i),
                row=i,
            )
            btn.callback = self._make_callback(item)
            self.add_item(btn)

    def _make_callback(self, item: Weapon | System):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.ctx.author.id:
                await interaction.response.send_message("❌ Not your choice to make.", ephemeral=True)
                return
            self.chosen = item
            await interaction.response.defer()
            self.stop()
        return callback


# ── Embed builders ───────────────────────────────────────────────────────────

_ACTIVATION_EMOJI = {
    "protocol":  "🔄",
    "quick":     "⚡",
    "full":      "🔵",
    "reaction":  "↩️",
    "invade":    "💻",
    "free":      "🆓",
}

def _act_emoji(activation: str) -> str:
    return _ACTIVATION_EMOJI.get(activation.lower(), "▶️")


def build_weapon_use_embed(
    char: LancerCharacter,
    weapon: Weapon,
    attack: AttackRollResult,
    damage_rolls: list[tuple[dict, DiceResult]],
    self_heat_rolls: list[DiceResult],
    accuracy: int,
    difficulty: int,
    crit_both: list | None = None,
) -> discord.Embed:
    """Full weapon-use embed: attack roll + damage + self-heat.

    crit_both: list of (damage_dict, winner_roll, loser_roll) when a crit
               occurred — used to show both rolls so the player can see the
               dice doubling. Pass None on a normal hit.
    """
    embed = discord.Embed(
        title=f"⚔️ {weapon.name}",
        color=_item_color(weapon),
    )

    # Weapon info line
    meta_parts = [f"**{weapon.weapon_type}**", f"Range: {weapon.range_str}"]
    if weapon.mount_size:
        meta_parts.append(f"Mount: {weapon.mount_size}")
    if weapon.source:
        meta_parts.append(f"*{weapon.source}*")
    embed.description = "  ·  ".join(meta_parts)

    # Attack roll
    attack_lines = [f"🎲 **{attack.d20}** (d20) + **{attack.grit}** (Grit)"]
    ar = attack.accuracy_result
    if ar.net != 0:
        kind = "Accuracy" if ar.net > 0 else "Difficulty"
        sign = "+" if ar.applied >= 0 else ""
        dice_str = ", ".join(str(r) for r in ar.rolls)
        attack_lines.append(
            f"{'🟢' if ar.net > 0 else '🔴'} {kind} ({abs(ar.net)}d6: [{dice_str}] → kept **{ar.kept}**) → {sign}{ar.applied}"
        )
    crit_str = "  🎯 **CRITICAL HIT!**" if attack.crit else ""
    attack_lines.append(f"**Attack Total: {attack.total}**{crit_str}")
    embed.add_field(name="🎯 Attack Roll", value="\n".join(attack_lines), inline=False)

    # Damage — crit path shows both rolls, normal path shows one
    if crit_both:
        # Crit: show each source with both dice rolls, highlight the winner
        dmg_lines = []
        for d, winner, loser in crit_both:
            ap = " **AP**" if d.get("ap") else ""
            target = " *(self)*" if d.get("target") == "self" else ""
            aoe_str = " *(AOE)*" if d.get("aoe") else ""

            def _fmt_roll(r: DiceResult) -> str:
                rolls_str = " + ".join(str(x) for x in r.rolls) if r.rolls else str(r.modifier)
                mod_str = f" + {r.modifier}" if r.modifier and r.rolls else ""
                return f"[{rolls_str}]{mod_str} = {r.total}"

            w_str = _fmt_roll(winner)
            l_str = _fmt_roll(loser)
            dmg_lines.append(
                f"**{winner.label}**{ap}{target}{aoe_str}:\n"
                f"  Roll 1: {w_str} ✅  |  Roll 2: {l_str}\n"
                f"  **Kept: {winner.total}**"
            )
        embed.add_field(name="💥 Damage (CRIT — doubled dice, kept highest)", value="\n".join(dmg_lines), inline=False)
    elif damage_rolls:
        dmg_lines = []
        for d, r in damage_rolls:
            ap = " **AP**" if d.get("ap") else ""
            target = " *(self)*" if d.get("target") == "self" else ""
            aoe_str = " *(AOE)*" if d.get("aoe") else ""
            rolls_str = " + ".join(str(x) for x in r.rolls) if r.rolls else str(r.modifier)
            mod_str = f" + {r.modifier}" if r.modifier and r.rolls else ""
            dmg_lines.append(
                f"**{r.label}**{ap}{target}{aoe_str}: [{rolls_str}]{mod_str} = **{r.total}**"
            )
        embed.add_field(name="💥 Damage", value="\n".join(dmg_lines), inline=False)

    # Self-heat from tags/weapon
    if self_heat_rolls:
        heat_lines = []
        for r in self_heat_rolls:
            rolls_str = " + ".join(str(x) for x in r.rolls) if r.rolls else str(r.modifier)
            mod_str = f" + {r.modifier}" if r.modifier and r.rolls else ""
            heat_lines.append(f"[{rolls_str}]{mod_str} = **{r.total}** ({r.label})")
        embed.add_field(name="🔥 Self Heat", value="\n".join(heat_lines), inline=False)

    # Effect / on_hit
    if weapon.effect:
        embed.add_field(name="📋 Effect", value=weapon.effect[:500], inline=False)
    if weapon.on_hit:
        embed.add_field(name="✅ On Hit", value=weapon.on_hit[:500], inline=False)

    # Tags
    if weapon.tags:
        tag_names = [format_tag(tag) for tag in weapon.tags]

    embed.add_field(
        name="🏷️ Tags",
        value=", ".join(tag_names),
        inline=True
    )

    embed.set_footer(text=f"{char.pilot.callsign} · {char.active_mech.name if char.active_mech else '?'}")
    return embed


def build_system_use_embed(
    char: LancerCharacter,
    system: System,
    action: SystemAction | None,
    annotated_text: str,
    damage_rolls: list[tuple[dict, DiceResult]],
    self_heat_rolls: list[DiceResult],
    accuracy: int,
    difficulty: int,
) -> discord.Embed:
    """System / action use embed with rolled dice."""
    embed = discord.Embed(
        title=f"🔧 {system.name}" + (f" — {action.name}" if action and action.name else ""),
        color=DEFAULT_COLOR,
    )

    # System meta
    meta_parts = []
    if system.type:
        meta_parts.append(f"**{system.type}**")
    if system.sp:
        meta_parts.append(f"SP: {system.sp}")
    if system.source:
        meta_parts.append(f"*{system.source}*")
    if meta_parts:
        embed.description = "  ·  ".join(meta_parts)

    # Activation type
    if action:
        act_str = f"{_act_emoji(action.activation)} **{action.activation} Action**"
        if action.frequency:
            act_str += f"  ·  {action.frequency}"
        embed.add_field(name="Activation", value=act_str, inline=False)

    # Annotated effect text (dice already substituted with rolls)
    if annotated_text:
        embed.add_field(name="📋 Effect", value=annotated_text[:1000], inline=False)

    # Accuracy/difficulty for Tech Attacks
    ar_label = None
    if accuracy > 0 or difficulty > 0:
        ar = roll_accuracy(accuracy, difficulty)
        kind = "Accuracy" if ar.net > 0 else "Difficulty" if ar.net < 0 else "Neutral"
        dice_str = ", ".join(str(r) for r in ar.rolls) if ar.rolls else "—"
        kept = f"kept **{ar.kept}**" if ar.rolls else "no dice"
        ar_label = f"{'🟢' if ar.net > 0 else '🔴' if ar.net < 0 else '⚪'} {kind} ({abs(ar.net)}d6: [{dice_str}] → {kept}) → **{ar.applied:+d}**"

    if ar_label:
        embed.add_field(name="🎲 Accuracy / Difficulty", value=ar_label, inline=False)

    # Damage
    if damage_rolls:
        dmg_lines = []
        for d, r in damage_rolls:
            ap = " **AP**" if d.get("ap") else ""
            target = " *(self)*" if d.get("target") == "self" else ""
            aoe_str = " *(AOE)*" if d.get("aoe") else ""
            rolls_str = " + ".join(str(x) for x in r.rolls) if r.rolls else str(r.modifier)
            mod_str = f" + {r.modifier}" if r.modifier and r.rolls else ""
            dmg_lines.append(
                f"**{r.label}**{ap}{target}{aoe_str}: [{rolls_str}]{mod_str} = **{r.total}**"
            )
        embed.add_field(name="💥 Damage", value="\n".join(dmg_lines), inline=False)

    if self_heat_rolls:
        heat_lines = []
        for r in self_heat_rolls:
            rolls_str = " + ".join(str(x) for x in r.rolls) if r.rolls else str(r.modifier)
            mod_str = f" + {r.modifier}" if r.modifier and r.rolls else ""
            heat_lines.append(f"[{rolls_str}]{mod_str} = **{r.total}** ({r.label})")
        embed.add_field(name="🔥 Self Heat", value="\n".join(heat_lines), inline=False)

    if system.tags:
        tag_names = [format_tag(tag) for tag in system.tags]

    embed.add_field(
        name="🏷️ Tags",
        value=", ".join(tag_names),
        inline=True,
    )

    embed.set_footer(text=f"{char.pilot.callsign} · {char.active_mech.name if char.active_mech else '?'}")
    return embed


# ── Cog ───────────────────────────────────────────────────────────────────────

class UseCog(commands.Cog, name="action"):
    """Unified action command for weapons and systems."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _parse_acc_diff(self, args: tuple[str, ...]) -> tuple[int, int]:
        """
        Parse accuracy / difficulty flags from extra args.
        Supported forms: acc 2 | a2 | accuracy 2 | diff 1 | d1 | difficulty 1
        """
        import re as _re
        acc, diff = 0, 0
        text = " ".join(args).lower()
        acc_m = _re.search(r"\b(?:acc(?:uracy)?)\s*(\d+)", text)
        diff_m = _re.search(r"\b(?:diff(?:iculty)?)\s*(\d+)", text)
        if not acc_m:
            acc_m = _re.search(r"\ba(\d+)\b", text)
        if not diff_m:
            diff_m = _re.search(r"\bd(\d+)\b", text)
        if acc_m:
            acc = int(acc_m.group(1))
        if diff_m:
            diff = int(diff_m.group(1))
        return acc, diff

    def _parse_item_name(self, args: tuple[str, ...]) -> str:
        """Strip accuracy/difficulty tokens from args and return the item name."""
        import re as _re
        text = " ".join(args)
        text = _re.sub(r"\b(?:acc(?:uracy)?|diff(?:iculty)?)\s*\d+", "", text, flags=_re.IGNORECASE)
        text = _re.sub(r"\b[ad]\d+\b", "", text)
        return text.strip()

    async def _resolve_item(
        self,
        ctx: commands.Context,
        query: str,
        accuracy: int,
        difficulty: int,
    ) -> Weapon | System | None:

        char = storage.load(ctx.guild.id, ctx.author.id)
        if not char:
            await ctx.reply(
                "❌ No character imported. Use `!import` first.",
                mention_author=False,
            )
            return None

        mech = char.active_mech
        if not mech:
            await ctx.reply(
                "❌ No active mech found.",
                mention_author=False,
            )
            return None

        if not query:
            all_items = _all_items(mech)

            lines = []
            for item in all_items:
                icon = "⚔️" if isinstance(item, Weapon) else "🔧"
                lines.append(f"{icon} **{item.name}**")

            embed = discord.Embed(
                title="🗂️ Available Actions",
                description="\n".join(lines) or "None equipped.",
                color=DEFAULT_COLOR,
            )

            embed.set_footer(
                text="Usage: !action <name> · !a <name> · acc 2 · diff 1"
            )

            await ctx.reply(embed=embed, mention_author=False)
            return None

        matches = _fuzzy_match(query, _all_items(mech))

        if not matches:
            await ctx.reply(
                f"❌ No weapon or system matching **\"{query}\"** found.\n"
                "Run `!action` with no arguments to list all available actions.",
                mention_author=False,
            )
            return None

        if len(matches) == 1:
            return matches[0]

        view = DisambiguateView(matches, ctx, accuracy, difficulty)

        embed = discord.Embed(
            title="🤔 Multiple matches found",
            description="\n".join(
                f"{'⚔️' if isinstance(m, Weapon) else '🔧'} **{m.name}**"
                for m in matches[:5]
            ),
            color=0xFFC107,
        )

        msg = await ctx.reply(
            embed=embed,
            view=view,
            mention_author=False,
        )

        await view.wait()

        if view.chosen is None:
            await msg.edit(
                content="⏱️ Selection timed out.",
                embed=None,
                view=None,
            )
            return None

        return view.chosen

    # ── !use ─────────────────────────────────────────────────────────────────

    @commands.command(
    name="action",
    aliases=["act","a"]
    )
    async def action_item(self, ctx: commands.Context, *args: str):
        """
        Unified weapon/system activation command.

        Examples:
            !a torch
            !a lightning generator
            !action beckoner acc 2
            !a lucifer diff 1
        """

        accuracy, difficulty = self._parse_acc_diff(args)
        item_name = self._parse_item_name(args)

        item = await self._resolve_item(
            ctx,
            item_name,
            accuracy,
            difficulty,
        )
        if item is None:
            return

        char = storage.load(ctx.guild.id, ctx.author.id)

        async with ctx.typing():
            if isinstance(item, Weapon):
                await self._use_weapon(
                    ctx,
                    char,
                    item,
                    accuracy,
                    difficulty,
                )
            else:
                await self._use_system(
                    ctx,
                    char,
                    item,
                    accuracy,
                    difficulty,
                )
    # ── Internal use logic ────────────────────────────────────────────────────

    async def _use_weapon(
        self,
        ctx,
        char,
        weapon,
        accuracy,
        difficulty,
    ):
        grit = char.pilot.stats.grit
        attack = roll_attack(grit, accuracy, difficulty)

        # ── Crit damage (Lancer rule: total >= 20 → roll dice twice, keep highest per source) ──
        if attack.crit:
            crit_triples = roll_damage_crit(weapon.damage)  # [(d, roll_a, roll_b), ...]
            # Pick the higher roll per source, build the normal damage_rolls list
            damage_rolls = []
            crit_both = []   # kept for display: (d, winner, loser)
            for d, roll_a, roll_b in crit_triples:
                winner, loser = (roll_a, roll_b) if roll_a.total >= roll_b.total else (roll_b, roll_a)
                damage_rolls.append((d, winner))
                crit_both.append((d, winner, loser))
        else:
            damage_rolls = _roll_damage(weapon.damage)
            crit_both = None

        self_heat_rolls = _total_self_heat(weapon)

        target_hp_dmg = sum(
            r.total for d, r in damage_rolls
            if d.get("target") != "self"
            and d.get("type", "").lower() != "heat"
        )
        target_heat = sum(
            r.total for d, r in damage_rolls
            if d.get("target") != "self"
            and d.get("type", "").lower() == "heat"
        )
        self_hp_dmg = sum(
            r.total for d, r in damage_rolls
            if d.get("target") == "self"
            and d.get("type", "").lower() != "heat"
        )
        self_heat = (
            sum(r.total for r in self_heat_rolls)
            + sum(
                r.total for d, r in damage_rolls
                if d.get("target") == "self"
                and d.get("type", "").lower() == "heat"
            )
        )

        embed = build_weapon_use_embed(
            char,
            weapon,
            attack,
            damage_rolls,
            self_heat_rolls,
            accuracy,
            difficulty,
            crit_both=crit_both,
        )
        embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)

        view = HeatHPView(
            guild_id=ctx.guild.id,
            user_id=ctx.author.id,
            hp_damage=target_hp_dmg,
            heat_gain=target_heat,
            self_hp_damage=self_hp_dmg,
            self_heat_gain=self_heat,
        )
        await ctx.reply(embed=embed, view=view, mention_author=False)
        

    async def _use_system(
        self,
        ctx: commands.Context,
        char: LancerCharacter,
        system: System,
        accuracy: int,
        difficulty: int,
    ):
        # If the system has multiple actions, ask which one
        action: SystemAction | None = None
        if len(system.actions) == 1:
            action = system.actions[0]
        elif len(system.actions) > 1:
            # Build a view with one button per action
            action_view = ActionSelectView(system.actions, ctx)
            act_lines = "\n".join(
                f"{_act_emoji(a.activation)} **{a.name or a.activation}** — {a.activation}"
                for a in system.actions
            )
            embed = discord.Embed(
                title=f"🔧 {system.name} — Choose an action",
                description=act_lines,
                color=DEFAULT_COLOR,
            )
            msg = await ctx.reply(embed=embed, view=action_view, mention_author=False)
            await action_view.wait()
            if action_view.chosen is None:
                await msg.edit(content="⏱️ Action selection timed out.", embed=None, view=None)
                return
            action = action_view.chosen

        # Determine text to roll dice in
        text_to_roll = ""
        if action:
            text_to_roll = action.detail
        elif system.effect:
            text_to_roll = system.effect

        annotated_text, _ = roll_all_dice_in_text(text_to_roll) if text_to_roll else ("", [])

        # Roll action damage separately for the buttons
        damage_rolls = _roll_damage(action.damage) if action else []
        self_heat_rolls = _total_self_heat(system, action)

        target_hp_dmg = sum(r.total for d, r in damage_rolls if d.get("target") != "self")
        self_hp_dmg   = sum(r.total for d, r in damage_rolls if d.get("target") == "self")
        target_heat   = sum(r.total for d, r in damage_rolls
                           if d.get("target") != "self" and d.get("type", "").lower() == "heat")
        self_heat     = sum(r.total for r in self_heat_rolls)

        embed = build_system_use_embed(
            char, system, action, annotated_text, damage_rolls, self_heat_rolls,
            accuracy, difficulty,
        )
        embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)

        view = HeatHPView(
            guild_id=ctx.guild.id,
            user_id=ctx.author.id,
            hp_damage=target_hp_dmg,
            heat_gain=target_heat,
            self_hp_damage=self_hp_dmg,
            self_heat_gain=self_heat,
        )

        await ctx.reply(embed=embed, view=view, mention_author=False)


class ActionSelectView(discord.ui.View):
    """Pick one action from a multi-action system."""

    def __init__(self, actions: list[SystemAction], ctx: commands.Context):
        super().__init__(timeout=30)
        self.chosen: SystemAction | None = None
        self.ctx = ctx

        for i, action in enumerate(actions[:5]):
            label = action.name or action.activation or f"Action {i+1}"
            btn = discord.ui.Button(
                label=f"{_act_emoji(action.activation)} {label}",
                style=discord.ButtonStyle.secondary,
                row=i,
            )
            btn.callback = self._make_callback(action)
            self.add_item(btn)

    def _make_callback(self, action: SystemAction):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.ctx.author.id:
                await interaction.response.send_message("Not your choice.", ephemeral=True)
                return
            self.chosen = action
            await interaction.response.defer()
            self.stop()
        return callback


async def setup(bot: commands.Bot):
    await bot.add_cog(UseCog(bot))
