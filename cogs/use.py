"""
Use cog — !action / !a for weapons and systems.

New in this version
───────────────────
-t <target> flag — target an NPC or player.
    !a torch -t barricade a hit an NPC; "Apply HP dmg" writes to NPC DB
    !a torch -t @Player hit a player; "Apply HP dmg" writes to their char
    !npca graviton lance -t @Player NPC attacks a player (same mechanism)

When a target is provided:
  • The embed shows a "Target" line with the target's current HP bar.
  • The "Apply HP dmg to target" button subtracts the rolled damage from the
    target's current_hp (NPC: npc_storage, player: storage/save_raw).
  • "+Heat to target" similarly applies heat to an NPC target.
  • Self-HP / self-heat buttons continue to affect the acting character.

Without -t the buttons behave exactly as before (manual tracking prompts).
"""
from __future__ import annotations
import json as _json
import re
import discord
from discord.ext import commands

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
from utils.tags import format_tag
import utils.storage as storage
from utils.lancer_checks import (
    roll_structure_check,
    roll_stress_check,
    attach_cascade,
    StructureResult,
    StressResult,
)
from utils.targeting import parse_target_flag, resolve_target, TargetResult

# ── colour constants ──────────────────────────────────────────────────────────
DAMAGE_COLORS = {
    "kinetic": 0x9E9E9E,
    "energy": 0x2196F3,
    "explosive": 0xE8871A,
    "burn": 0xFF5722,
    "heat": 0xFF9800,
    "variable": 0x9C27B0,
}
DEFAULT_COLOR = 0xCF2020


def _item_color(item: Weapon | System) -> int:
    if isinstance(item, Weapon) and item.damage:
        dtype = item.damage[0].get("type", "").lower()
        return DAMAGE_COLORS.get(dtype, DEFAULT_COLOR)
    return DEFAULT_COLOR


def _progress_bar(current: int, maximum: int, length: int = 10) -> str:
    if maximum <= 0:
        return "█"* length
    filled = round(length * max(0, current) / maximum)
    return "█"* max(0, min(filled, length)) + "░"* max(0, length - filled)


# ── fuzzy search ──────────────────────────────────────────────────────────────

def _fuzzy_match(query: str, items: list[Weapon | System]) -> list[Weapon | System]:
    q = query.strip().lower()
    words = q.split()
    multi = [i for i in items if all(w in i.name.lower() for w in words)]
    if multi:
        return multi
    return [i for i in items if q in i.name.lower()]


def _all_items(mech) -> list[Weapon | System]:
    return list(mech.weapons) + list(mech.systems)


# ── damage roll helpers ───────────────────────────────────────────────────────

def _roll_damage(damage_list: list[dict]) -> list[tuple[dict, DiceResult]]:
    results = []
    for d in damage_list:
        val = str(d.get("val", "0"))
        dtype = d.get("type", "?")
        results.append((d, roll_expression(val, label=dtype)))
    return results


def _total_self_heat(
    item: Weapon | System,
    action: SystemAction | None = None,
) -> list[DiceResult]:
    heat_results = []
    hs = item.heat_self()
    if hs is not None:
        heat_results.append(roll_expression(str(hs), label="Self Heat (tag)"))
    if action:
        for d in action.damage:
            if d.get("target") == "self" and d.get("type", "").lower() == "heat":
                heat_results.append(
                    roll_expression(str(d.get("val", 0)), label="Self Heat (action)")
                )
    return heat_results


# ── NPC target helpers ────────────────────────────────────────────────────────

def _apply_hp_to_npc(
    guild_id: int, channel_id: int,
    slug: str, damage: int,
) -> tuple[int, int, int, bool]:
    """
    Subtract damage from an NPC's HP. Returns (old_hp, new_hp, max_hp, killed).
    """
    import utils.npc_storage as npc_storage
    enemy = npc_storage.get_enemy(guild_id, channel_id, slug)
    if enemy is None:
        return 0, 0, 0, False
    s = enemy.stats
    old_hp = s.current_hp
    new_hp = max(0, old_hp - damage)
    killed = new_hp == 0 and s.current_structure <= 1
    npc_storage.update_enemy_vitals(
        guild_id, channel_id, slug,
        hp=new_hp,
        is_dead=killed if killed else None,
    )
    return old_hp, new_hp, s.hp, killed


def _apply_heat_to_npc(
    guild_id: int, channel_id: int,
    slug: str, heat_amount: int,
) -> tuple[int, int, int, bool]:
    """
    Add heat to an NPC. Returns (old_heat, new_heat, heatcap, overloaded).
    """
    import utils.npc_storage as npc_storage
    enemy = npc_storage.get_enemy(guild_id, channel_id, slug)
    if enemy is None:
        return 0, 0, 0, False
    s = enemy.stats
    old_heat = s.current_heat
    new_raw = old_heat + heat_amount
    overloaded = new_raw > s.heatcap and s.heatcap > 0
    new_heat = max(0, min(s.heatcap, new_raw))
    npc_storage.update_enemy_vitals(guild_id, channel_id, slug, heat=new_heat)
    return old_heat, new_heat, s.heatcap, overloaded


def _apply_hp_to_player(
    guild_id: int, user_id: int, damage: int,
) -> tuple[int, int, int]:
    """
    Subtract damage from a player's mech HP. Returns (old_hp, new_hp, max_hp).
    Triggers structure check if HP hits 0 (stores result but doesn't post embed).
    """
    raw = storage.load_raw(guild_id, user_id)
    if raw is None:
        return 0, 0, 0
    char = storage.load(guild_id, user_id)
    if char is None or char.active_mech is None:
        return 0, 0, 0
    data = _json.loads(raw)
    ms = char.active_mech.stats
    cur = data["data"]["mechs"][0]["stats"]["current"]
    old_hp = cur.get("hp", ms.hp)
    new_hp = max(0, old_hp - damage)
    cur["hp"] = new_hp
    storage.save_raw(guild_id, user_id, char.pilot.callsign, _json.dumps(data))
    return old_hp, new_hp, ms.hp


# ═════════════════════════════════════════════════════════════════════════════
# HeatHPView — interactive buttons on weapon/system use embeds
# ═════════════════════════════════════════════════════════════════════════════

class HeatHPView(discord.ui.View):
    """
    Buttons to apply damage / heat. When a TargetResult is provided the
    relevant buttons write directly to the NPC or player DB and update the
    embed in-place. Without a target they fall back to manual-tracking prompts.

    Layout
    ──────
    Row 0 [Apply HP dmg → target] [+Heat → target]
    Row 1 [Apply self HP dmg] [+Self Heat]
    """

    def __init__(
        self,
        guild_id: int,
        channel_id: int,
        user_id: int, # the acting player's Discord ID
        hp_damage: int = 0, # damage dealt to target
        heat_gain: int = 0, # heat applied to target
        self_hp_damage: int = 0,
        self_heat_gain: int = 0,
        target: TargetResult | None = None,
        embed: discord.Embed | None = None, # live reference for in-place edit
    ):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.user_id = user_id
        self.hp_damage = hp_damage
        self.heat_gain = heat_gain
        self.self_hp_damage = self_hp_damage
        self.self_heat_gain = self_heat_gain
        self.target = target
        self.embed = embed

        # ── Row 0: target buttons ─────────────────────────────────────────────
        if hp_damage <= 0:
            self.apply_hp.disabled = True
            self.apply_hp.label = "No HP damage"
        else:
            tname = target.label if target else "target"
            self.apply_hp.label = f"Apply {hp_damage} dmg → {tname}"

        if heat_gain <= 0:
            self.apply_heat.disabled = True
            self.apply_heat.label = "No heat to target"
        else:
            tname = target.label if target else "target"
            self.apply_heat.label = f"+{heat_gain} Heat → {tname}"

        # ── Row 1: self buttons ───────────────────────────────────────────────
        if self_hp_damage <= 0:
            self.apply_self_hp.disabled = True
            self.apply_self_hp.label = "No self HP"
        else:
            self.apply_self_hp.label = f"{self_hp_damage} self HP dmg"

        if self_heat_gain <= 0:
            self.apply_self_heat.disabled = True
            self.apply_self_heat.label = "No self Heat"
        else:
            self.apply_self_heat.label = f"+{self_heat_gain} self Heat"

    # ── Patch embed target field in-place ─────────────────────────────────────

    def _patch_embed_field(self, name: str, value: str) -> None:
        """Update or append a named field on self.embed."""
        if self.embed is None:
            return
        for i, f in enumerate(self.embed.fields):
            if f.name == name:
                self.embed.set_field_at(i, name=name, value=value, inline=False)
                return
        self.embed.add_field(name=name, value=value, inline=False)

    # ─────────────────────────────────────────────────────────────────────────
    # Row 0 — target HP
    # ─────────────────────────────────────────────────────────────────────────

    @discord.ui.button(style=discord.ButtonStyle.danger, row=0)
    async def apply_hp(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if self.target is None:
            # No target — manual prompt
            await interaction.response.send_message(
                f"Deal **{self.hp_damage} HP damage** to your target manually.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        if self.target.kind == "npc":
            old, new, maxhp, killed = _apply_hp_to_npc(
                self.guild_id, self.channel_id,
                self.target.npc_slug, self.hp_damage,
            )
            bar = _progress_bar(new, maxhp)
            status = "**DESTROYED**" if killed else f"**{old} → {new}/{maxhp}**"
            field_val = f"`{bar}` {status}"
            if killed:
                field_val += " — removed from play."

        else: # player
            old, new, maxhp = _apply_hp_to_player(
                self.guild_id, self.target.player_user_id, self.hp_damage,
            )
            bar = _progress_bar(new, maxhp)
            field_val = f"`{bar}` **{old} → {new}/{maxhp}**"

        button.disabled = True
        button.label = f"{self.hp_damage} dmg applied"
        button.style = discord.ButtonStyle.success
        self._patch_embed_field(
            f"{self.target.label} HP",
            field_val,
        )
        await interaction.edit_original_response(embed=self.embed, view=self)

    # ─────────────────────────────────────────────────────────────────────────
    # Row 0 — target Heat (NPC-only; players don't store heat externally yet)
    # ─────────────────────────────────────────────────────────────────────────

    @discord.ui.button(style=discord.ButtonStyle.secondary, row=0)
    async def apply_heat(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if self.target is None or self.target.kind != "npc":
            await interaction.response.send_message(
                f"Apply **{self.heat_gain} Heat** to your target manually.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        old, new, heatcap, overloaded = _apply_heat_to_npc(
            self.guild_id, self.channel_id,
            self.target.npc_slug, self.heat_gain,
        )
        bar = _progress_bar(new, heatcap)
        dz = "**DANGER ZONE**" if new >= heatcap // 2 else ""
        overheat_str = "\n **OVERHEATED** — NPC takes 2 AP Energy, Impaired/Slowed." if overloaded else ""
        field_val = f"`{bar}` **{old} → {new}/{heatcap}**{dz}{overheat_str}"

        button.disabled = True
        button.label = f"+{self.heat_gain} heat applied"
        button.style = discord.ButtonStyle.success
        self._patch_embed_field(
            f"{self.target.label} Heat",
            field_val,
        )
        await interaction.edit_original_response(embed=self.embed, view=self)

    # ─────────────────────────────────────────────────────────────────────────
    # Row 1 — self HP (acting player's mech)
    # ─────────────────────────────────────────────────────────────────────────

    @discord.ui.button(style=discord.ButtonStyle.danger, row=1)
    async def apply_self_hp(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        char = storage.load(self.guild_id, self.user_id)
        if not char or not char.active_mech:
            await interaction.response.send_message(
                "Character not found.", ephemeral=True
            )
            return

        raw = storage.load_raw(self.guild_id, self.user_id)
        data = _json.loads(raw)
        cur = data["data"]["mechs"][0]["stats"]["current"]
        ms = char.active_mech.stats

        old_hp = cur.get("hp", ms.hp)
        new_hp = old_hp - self.self_hp_damage
        overflow = max(0, -new_hp)

        structure_result: StructureResult | None = None
        if new_hp <= 0 and ms.current_structure > 0:
            from cogs.use_helpers import _run_structure_check # lazy import avoids cycle
            structure_result = roll_structure_check(
                structure_before=cur.get("structure", ms.structure),
                max_structure=ms.structure,
                hp_overflow=overflow,
            )
            attach_cascade(structure_result, char.active_mech)
            new_structure = max(0, structure_result.structure_after)
            cur["structure"] = new_structure
            cur["hp"] = max(0, ms.hp - overflow) if new_structure > 0 else 0
        else:
            cur["hp"] = max(0, new_hp)

        storage.save_raw(
            self.guild_id, self.user_id,
            char.pilot.callsign, _json.dumps(data),
        )
        button.disabled = True
        await interaction.response.defer()

        hp_msg = (
            f"HP: **{old_hp}** → **{max(0,new_hp)}/{ms.hp}**"
            if not structure_result
            else f"HP: **{old_hp}** → **0** (−{self.self_hp_damage})"
              + (f" overflow: **{overflow}**" if overflow else "")
        )
        await interaction.followup.send(hp_msg)

        if structure_result:
            from cogs.encounter import _build_structure_embed
            await interaction.followup.send(
                embed=_build_structure_embed(structure_result, char.active_mech)
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Row 1 — self Heat
    # ─────────────────────────────────────────────────────────────────────────

    @discord.ui.button(style=discord.ButtonStyle.primary, row=1)
    async def apply_self_heat(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        char = storage.load(self.guild_id, self.user_id)
        if not char or not char.active_mech:
            await interaction.response.send_message(
                "Character not found.", ephemeral=True
            )
            return

        raw = storage.load_raw(self.guild_id, self.user_id)
        data = _json.loads(raw)
        cur = data["data"]["mechs"][0]["stats"]["current"]
        ms = char.active_mech.stats

        old_heat = cur.get("heat", 0)
        new_heat_raw = old_heat + self.self_heat_gain
        overflow = max(0, new_heat_raw - ms.heatcap)

        stress_result: StressResult | None = None
        if new_heat_raw > ms.heatcap and cur.get("stress", ms.stress) > 0:
            stress_result = roll_stress_check(
                stress_before=cur.get("stress", ms.stress),
                max_stress=ms.stress,
                heat_overflow=overflow,
            )
            attach_cascade(stress_result, char.active_mech)
            cur["stress"] = max(0, stress_result.stress_after)
            cur["heat"] = overflow
        else:
            cur["heat"] = min(ms.heatcap, new_heat_raw)

        storage.save_raw(
            self.guild_id, self.user_id,
            char.pilot.callsign, _json.dumps(data),
        )
        button.disabled = True
        await interaction.response.defer()

        final_heat = cur["heat"]
        if stress_result:
            heat_msg = (
                f"Heat: **{old_heat}** → **{ms.heatcap}** (OVERLOAD!)"
                + (f" overflow: **{overflow}** carried forward" if overflow else "")
            )
        else:
            dz = "**DANGER ZONE!**" if final_heat >= ms.heatcap // 2 else ""
            heat_msg = (
                f"Heat: **{old_heat}** → **{final_heat}/{ms.heatcap}** "
                f"(+{self.self_heat_gain}){dz}"
            )
        await interaction.followup.send(heat_msg)

        if stress_result:
            from cogs.encounter import _build_stress_embed
            await interaction.followup.send(
                embed=_build_stress_embed(stress_result, char.active_mech)
            )


# ─────────────────────────────────────────────────────────────────────────────
# Disambiguation view
# ─────────────────────────────────────────────────────────────────────────────

class DisambiguateView(discord.ui.View):
    def __init__(
        self,
        items: list[Weapon | System],
        ctx: commands.Context,
        accuracy: int,
        difficulty: int,
    ):
        super().__init__(timeout=30)
        self.chosen: Weapon | System | None = None
        self.ctx = ctx
        self.accuracy = accuracy
        self.difficulty = difficulty
        for i, item in enumerate(items[:5]):
            kind = "" if isinstance(item, Weapon) else ""
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
                await interaction.response.send_message(
                    "Not your choice to make.", ephemeral=True
                )
                return
            self.chosen = item
            await interaction.response.defer()
            self.stop()
        return callback


# ─────────────────────────────────────────────────────────────────────────────
# Embed builders
# ─────────────────────────────────────────────────────────────────────────────

_ACTIVATION_EMOJI = {
    "protocol": "",
    "quick": "[*]",
    "full": "",
    "reaction": "↩️",
    "invade": "",
    "free": "",
}

def _act_emoji(activation: str) -> str:
    return _ACTIVATION_EMOJI.get(activation.lower(), "")


def build_weapon_use_embed(
    char: LancerCharacter,
    weapon: Weapon,
    attack: AttackRollResult,
    damage_rolls: list[tuple[dict, DiceResult]],
    self_heat_rolls: list[DiceResult],
    accuracy: int,
    difficulty: int,
    crit_both: list | None = None,
    target: TargetResult | None = None,
    dmg_bonus: int = 0,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"{weapon.name}",
        color=_item_color(weapon),
    )

    meta_parts = [f"**{weapon.weapon_type}**", f"Range: {weapon.range_str}"]
    if weapon.mount_size:
        meta_parts.append(f"Mount: {weapon.mount_size}")
    if weapon.source:
        meta_parts.append(f"*{weapon.source}*")
    embed.description = " · ".join(meta_parts)

    # ── Target preview ────────────────────────────────────────────────────────
    if target:
        if target.kind == "npc":
            bar = _progress_bar(target.npc_hp, target.npc_max_hp)
            embed.add_field(
                name=f"Target: {target.label}",
                value=f" `{bar}` {target.npc_hp}/{target.npc_max_hp} HP",
                inline=False,
            )
        else:
            embed.add_field(
                name=f"Target: {target.label}",
                value="Player — HP tracked on their sheet.",
                inline=False,
            )

    # ── Attack roll ───────────────────────────────────────────────────────────
    attack_lines = [f"**{attack.d20}** (d20) + **{attack.grit}** (Grit)"]
    ar = attack.accuracy_result
    if ar.net != 0:
        kind = "Accuracy" if ar.net > 0 else "Difficulty"
        sign = "+" if ar.applied >= 0 else ""
        dice_str = ", ".join(str(r) for r in ar.rolls)
        attack_lines.append(
            f"{'' if ar.net > 0 else ''} {kind} "
            f"({abs(ar.net)}d6: [{dice_str}] → kept **{ar.kept}**) → {sign}{ar.applied}"
        )
    crit_str = "**CRITICAL HIT!**" if attack.crit else ""
    attack_lines.append(f"**Attack Total: {attack.total}**{crit_str}")
    embed.add_field(name="Attack Roll", value="\n".join(attack_lines), inline=False)

    # ── Damage ────────────────────────────────────────────────────────────────
    if crit_both:
        dmg_lines = []
        for d, winner, loser in crit_both:
            ap = "**AP**" if d.get("ap") else ""
            target_str = "*(self)*" if d.get("target") == "self" else ""
            aoe_str = "*(AOE)*" if d.get("aoe") else ""

            def _fmt(r: DiceResult) -> str:
                rolls_str = "+ ".join(str(x) for x in r.rolls) if r.rolls else str(r.modifier)
                mod_str = f"+ {r.modifier}" if r.modifier and r.rolls else ""
                return f"[{rolls_str}]{mod_str} = {r.total}"

            dmg_lines.append(
                f"**{winner.label}**{ap}{target_str}{aoe_str}:\n"
                f"Roll 1: {_fmt(winner)} | Roll 2: {_fmt(loser)}\n"
                f"**Kept: {winner.total}**"
            )
        embed.add_field(
            name="Damage (CRIT — doubled dice, kept highest)",
            value="\n".join(dmg_lines),
            inline=False,
        )
    elif damage_rolls:
        dmg_lines = []
        for d, r in damage_rolls:
            ap = "**AP**" if d.get("ap") else ""
            target_str = "*(self)*" if d.get("target") == "self" else ""
            aoe_str = "*(AOE)*" if d.get("aoe") else ""
            rolls_str = "+ ".join(str(x) for x in r.rolls) if r.rolls else str(r.modifier)
            mod_str = f"+ {r.modifier}" if r.modifier and r.rolls else ""
            dmg_lines.append(
                f"**{r.label}**{ap}{target_str}{aoe_str}: "
                f"[{rolls_str}]{mod_str} = **{r.total}**"
            )
        if dmg_bonus:
            dmg_lines.append(f"**Bonus**: +{dmg_bonus} (manual)")
        embed.add_field(name="Damage", value="\n".join(dmg_lines), inline=False)

    # ── Self-heat ─────────────────────────────────────────────────────────────
    if self_heat_rolls:
        heat_lines = []
        for r in self_heat_rolls:
            rolls_str = "+ ".join(str(x) for x in r.rolls) if r.rolls else str(r.modifier)
            mod_str = f"+ {r.modifier}" if r.modifier and r.rolls else ""
            heat_lines.append(f"[{rolls_str}]{mod_str} = **{r.total}** ({r.label})")
        embed.add_field(name="Self Heat", value="\n".join(heat_lines), inline=False)

    if weapon.effect:
        embed.add_field(name="Effect", value=weapon.effect[:500], inline=False)
    if weapon.on_hit:
        embed.add_field(name="On Hit", value=weapon.on_hit[:500], inline=False)

    if weapon.tags:
        tag_names = [format_tag(tag) for tag in weapon.tags]
        embed.add_field(name="Tags", value=", ".join(tag_names), inline=True)

    embed.set_footer(
        text=f"{char.pilot.callsign} · "
             f"{char.active_mech.name if char.active_mech else '?'}"
             + (f" → {target.label}" if target else "")
    )
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
    target: TargetResult | None = None,
    dmg_bonus: int = 0,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"{system.name}"
              + (f" — {action.name}" if action and action.name else ""),
        color=DEFAULT_COLOR,
    )

    meta_parts = []
    if system.type:
        meta_parts.append(f"**{system.type}**")
    if system.sp:
        meta_parts.append(f"SP: {system.sp}")
    if system.source:
        meta_parts.append(f"*{system.source}*")
    if meta_parts:
        embed.description = " · ".join(meta_parts)

    # ── Target preview ────────────────────────────────────────────────────────
    if target:
        if target.kind == "npc":
            bar = _progress_bar(target.npc_hp, target.npc_max_hp)
            embed.add_field(
                name=f"Target: {target.label}",
                value=f" `{bar}` {target.npc_hp}/{target.npc_max_hp} HP",
                inline=False,
            )
        else:
            embed.add_field(
                name=f"Target: {target.label}",
                value="Player — HP tracked on their sheet.",
                inline=False,
            )

    if action:
        act_str = f"{_act_emoji(action.activation)} **{action.activation} Action**"
        if action.frequency:
            act_str += f" · {action.frequency}"
        embed.add_field(name="Activation", value=act_str, inline=False)

    if annotated_text:
        embed.add_field(name="Effect", value=annotated_text[:1000], inline=False)

    ar_label = None
    if accuracy > 0 or difficulty > 0:
        ar = roll_accuracy(accuracy, difficulty)
        kind = "Accuracy" if ar.net > 0 else "Difficulty" if ar.net < 0 else "Neutral"
        dice_str = ", ".join(str(r) for r in ar.rolls) if ar.rolls else "—"
        kept = f"kept **{ar.kept}**" if ar.rolls else "no dice"
        ar_label = (
            f"{'' if ar.net > 0 else '' if ar.net < 0 else ''} {kind} "
            f"({abs(ar.net)}d6: [{dice_str}] → {kept}) → **{ar.applied:+d}**"
        )
    if ar_label:
        embed.add_field(name="Accuracy / Difficulty", value=ar_label, inline=False)

    if damage_rolls:
        dmg_lines = []
        for d, r in damage_rolls:
            ap = "**AP**" if d.get("ap") else ""
            target_str = "*(self)*" if d.get("target") == "self" else ""
            aoe_str = "*(AOE)*" if d.get("aoe") else ""
            rolls_str = "+ ".join(str(x) for x in r.rolls) if r.rolls else str(r.modifier)
            mod_str = f"+ {r.modifier}" if r.modifier and r.rolls else ""
            dmg_lines.append(
                f"**{r.label}**{ap}{target_str}{aoe_str}: "
                f"[{rolls_str}]{mod_str} = **{r.total}**"
            )
        if dmg_bonus:
            dmg_lines.append(f"**Bonus**: +{dmg_bonus} (manual)")
        embed.add_field(name="Damage", value="\n".join(dmg_lines), inline=False)

    if self_heat_rolls:
        heat_lines = []
        for r in self_heat_rolls:
            rolls_str = "+ ".join(str(x) for x in r.rolls) if r.rolls else str(r.modifier)
            mod_str = f"+ {r.modifier}" if r.modifier and r.rolls else ""
            heat_lines.append(f"[{rolls_str}]{mod_str} = **{r.total}** ({r.label})")
        embed.add_field(name="Self Heat", value="\n".join(heat_lines), inline=False)

    if system.tags:
        tag_names = [format_tag(tag) for tag in system.tags]
        embed.add_field(name="Tags", value=", ".join(tag_names), inline=True)

    embed.set_footer(
        text=f"{char.pilot.callsign} · "
             f"{char.active_mech.name if char.active_mech else '?'}"
             + (f" → {target.label}" if target else "")
    )
    return embed


# ─────────────────────────────────────────────────────────────────────────────
# ActionSelectView
# ─────────────────────────────────────────────────────────────────────────────

class ActionSelectView(discord.ui.View):
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
                await interaction.response.send_message(
                    "Not your choice.", ephemeral=True
                )
                return
            self.chosen = action
            await interaction.response.defer()
            self.stop()
        return callback


# ═════════════════════════════════════════════════════════════════════════════
# Cog
# ═════════════════════════════════════════════════════════════════════════════

class UseCog(commands.Cog, name="action"):
    """Unified action command for weapons and systems."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── flag parsers ──────────────────────────────────────────────────────────

    def _parse_acc_diff(self, text: str) -> tuple[int, int]:
        acc, diff = 0, 0
        acc_m = re.search(r"\b(?:acc(?:uracy)?)\s*(\d+)", text)
        diff_m = re.search(r"\b(?:diff(?:iculty)?)\s*(\d+)", text)
        if not acc_m:
            acc_m = re.search(r"\ba(\d+)\b", text)
        if not diff_m:
            diff_m = re.search(r"\bd(\d+)\b", text)
        if acc_m:
            acc = int(acc_m.group(1))
        if diff_m:
            diff = int(diff_m.group(1))
        return acc, diff

    def _strip_flags(self, args: tuple[str, ...]) -> tuple[str, int, int, str, int]:
        """
        Strip -t, -d, acc/diff flags from args.
        Returns (item_name, accuracy, difficulty, raw_target_query, dmg_bonus).
        """
        text = " ".join(args)

        # Pull -d / --damage flag first
        dmg_bonus = 0
        dmg_m = re.search(r"(?:--|-)d(?:amage)?\s+(\d+)", text, re.IGNORECASE)
        if dmg_m:
            dmg_bonus = int(dmg_m.group(1))
            text = (text[:dmg_m.start()] + text[dmg_m.end():]).strip()

        # Pull -t flag (before acc parsing so 'a2' in target name isn't eaten)
        text, raw_target = parse_target_flag(text)

        # Acc / diff
        acc, diff = self._parse_acc_diff(text)
        text = re.sub(r"\b(?:acc(?:uracy)?)\s*\d+", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\b(?:diff(?:iculty)?)\s*\d+", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\ba\d+\b", "", text)
        text = re.sub(r"\bd\d+\b", "", text)

        return text.strip(), acc, diff, raw_target, dmg_bonus

    # ── !action ───────────────────────────────────────────────────────────────

    @commands.command(name="action", aliases=["act", "a"])
    async def action_item(self, ctx: commands.Context, *args: str):
        """
        Activate a weapon or system.

        Usage
        ─────
            !a torch
            !a lightning generator
            !a beckoner acc 2
            !a lucifer diff 1
            !a torch -t barricade a          -- target NPC; button applies damage
            !a torch -t @Player              -- target player
            !a torch -t barricade a -d 3     -- +3 manual bonus damage on top of roll
        """
        item_name, accuracy, difficulty, raw_target, dmg_bonus = self._strip_flags(args)

        target: TargetResult | None = None
        if raw_target:
            guild_members = {
                m.id: m.display_name
                for m in ctx.guild.members
            } if ctx.guild else {}
            target = resolve_target(
                ctx.guild.id, ctx.channel.id,
                raw_target, guild_members,
            )
            if target is None:
                await ctx.reply(
                    f"Could not resolve target \"{raw_target}\". "
                    "Check the name and try again. Continuing without target.",
                    mention_author=False,
                )

        item = await self._resolve_item(ctx, item_name, accuracy, difficulty)
        if item is None:
            return

        char = storage.load(ctx.guild.id, ctx.author.id)

        async with ctx.typing():
            if isinstance(item, Weapon):
                await self._use_weapon(ctx, char, item, accuracy, difficulty, target, dmg_bonus)
            else:
                await self._use_system(ctx, char, item, accuracy, difficulty, target, dmg_bonus)

    # ── item resolution ───────────────────────────────────────────────────────

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
                "No character imported. Use `!import` first.",
                mention_author=False,
            )
            return None

        mech = char.active_mech
        if not mech:
            await ctx.reply("No active mech found.", mention_author=False)
            return None

        if not query:
            all_items = _all_items(mech)
            lines = [
                f"{'' if isinstance(i, Weapon) else ''} **{i.name}**"
                for i in all_items
            ]
            embed = discord.Embed(
                title="Available Actions",
                description="\n".join(lines) or "None equipped.",
                color=DEFAULT_COLOR,
            )
            embed.set_footer(
                text="Usage: !action <name> · acc 2 · diff 1 · -t <target>"
            )
            await ctx.reply(embed=embed, mention_author=False)
            return None

        matches = _fuzzy_match(query, _all_items(mech))

        if not matches:
            await ctx.reply(
                f"No weapon or system matching **\"{query}\"** found.\n"
                "Run `!action` with no arguments to list all available actions.",
                mention_author=False,
            )
            return None

        if len(matches) == 1:
            return matches[0]

        view = DisambiguateView(matches, ctx, accuracy, difficulty)
        embed = discord.Embed(
            title="Multiple matches found",
            description="\n".join(
                f"{'' if isinstance(m, Weapon) else ''} **{m.name}**"
                for m in matches[:5]
            ),
            color=0xFFC107,
        )
        msg = await ctx.reply(embed=embed, view=view, mention_author=False)
        await view.wait()

        if view.chosen is None:
            await msg.edit(content="Selection timed out.", embed=None, view=None)
            return None

        return view.chosen

    # ── weapon use ────────────────────────────────────────────────────────────

    async def _use_weapon(
        self,
        ctx: commands.Context,
        char: LancerCharacter,
        weapon: Weapon,
        accuracy: int,
        difficulty: int,
        target: TargetResult | None,
        dmg_bonus: int = 0,
    ) -> None:
        grit   = char.pilot.stats.grit
        attack = roll_attack(grit, accuracy, difficulty)

        if attack.crit:
            crit_triples = roll_damage_crit(weapon.damage)
            damage_rolls = []
            crit_both    = []
            for d, roll_a, roll_b in crit_triples:
                winner, loser = (
                    (roll_a, roll_b) if roll_a.total >= roll_b.total
                    else (roll_b, roll_a)
                )
                damage_rolls.append((d, winner))
                crit_both.append((d, winner, loser))
        else:
            damage_rolls = _roll_damage(weapon.damage)
            crit_both    = None

        self_heat_rolls = _total_self_heat(weapon)

        target_hp_dmg = sum(
            r.total for d, r in damage_rolls
            if d.get("target") != "self" and d.get("type", "").lower() != "heat"
        ) + dmg_bonus
        target_heat = sum(
            r.total for d, r in damage_rolls
            if d.get("target") != "self" and d.get("type", "").lower() == "heat"
        )
        self_hp_dmg = sum(
            r.total for d, r in damage_rolls
            if d.get("target") == "self" and d.get("type", "").lower() != "heat"
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
            char, weapon, attack, damage_rolls, self_heat_rolls,
            accuracy, difficulty,
            crit_both=crit_both,
            target=target,
            dmg_bonus=dmg_bonus,
        )
        embed.set_author(
            name=ctx.author.display_name,
            icon_url=ctx.author.display_avatar.url,
        )

        view = HeatHPView(
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id,
            user_id=ctx.author.id,
            hp_damage=target_hp_dmg,
            heat_gain=target_heat,
            self_hp_damage=self_hp_dmg,
            self_heat_gain=self_heat,
            target=target,
            embed=embed,
        )
        await ctx.reply(embed=embed, view=view, mention_author=False)

    # ── system use ────────────────────────────────────────────────────────────

    async def _use_system(
        self,
        ctx: commands.Context,
        char: LancerCharacter,
        system: System,
        accuracy: int,
        difficulty: int,
        target: TargetResult | None,
        dmg_bonus: int = 0,
    ) -> None:
        action: SystemAction | None = None

        if len(system.actions) == 1:
            action = system.actions[0]
        elif len(system.actions) > 1:
            action_view = ActionSelectView(system.actions, ctx)
            act_lines = "\n".join(
                f"{_act_emoji(a.activation)} **{a.name or a.activation}** — {a.activation}"
                for a in system.actions
            )
            embed = discord.Embed(
                title=f"{system.name} — Choose an action",
                description=act_lines,
                color=DEFAULT_COLOR,
            )
            msg = await ctx.reply(embed=embed, view=action_view, mention_author=False)
            await action_view.wait()
            if action_view.chosen is None:
                await msg.edit(
                    content="Action selection timed out.",
                    embed=None, view=None,
                )
                return
            action = action_view.chosen

        text_to_roll = (action.detail if action else "") or system.effect or ""
        annotated_text, _ = (
            roll_all_dice_in_text(text_to_roll) if text_to_roll else ("", [])
        )

        damage_rolls    = _roll_damage(action.damage) if action else []
        self_heat_rolls = _total_self_heat(system, action)

        target_hp_dmg = sum(
            r.total for d, r in damage_rolls if d.get("target") != "self"
        ) + dmg_bonus
        self_hp_dmg = sum(r.total for d, r in damage_rolls if d.get("target") == "self")
        target_heat = sum(
            r.total for d, r in damage_rolls
            if d.get("target") != "self" and d.get("type", "").lower() == "heat"
        )
        self_heat = sum(r.total for r in self_heat_rolls)

        embed = build_system_use_embed(
            char, system, action, annotated_text,
            damage_rolls, self_heat_rolls,
            accuracy, difficulty,
            target=target,
            dmg_bonus=dmg_bonus,
        )
        embed.set_author(
            name=ctx.author.display_name,
            icon_url=ctx.author.display_avatar.url,
        )

        view = HeatHPView(
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id,
            user_id=ctx.author.id,
            hp_damage=target_hp_dmg,
            heat_gain=target_heat,
            self_hp_damage=self_hp_dmg,
            self_heat_gain=self_heat,
            target=target,
            embed=embed,
        )
        await ctx.reply(embed=embed, view=view, mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(UseCog(bot))
