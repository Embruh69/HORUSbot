"""
Encounter cog — GM commands for managing NPC enemies.

Command surface
───────────────
!npc add [-qty N]             — attach 1+ NPC JSON files
!npc list                     — encounter roster
!npc show <name>              — full statblock
!npc activate [name…]         — mark enemies active (interactive or by name)
!npc deactivate               — clear active markers
!npc hp <name> [amount]       — adjust/show HP
!npc heat <name> [amount]     — adjust/show heat
!npc kill <name>              — mark destroyed
!npc fr <name>                — full repair
!npc clear                    — wipe encounter

!npca <enemy> [feature]       — use a weapon or system as an NPC
  • No feature arg → interactive button picker of all features
  • Weapons → attack roll (1d20 + atk_bonus ± accuracy) + damage
  • Techs   → tech attack roll (1d20 + tech_attack ± accuracy) + effect
  • Systems / Traits / Reactions → display effect, roll inline dice
  • Self-heat button on weapon/tech results to track NPC heat
"""
from __future__ import annotations
import re
import random
import discord
from discord.ext import commands

import utils.npc_storage as npc_storage
from utils.npc_parser import NpcEnemy, NpcWeapon, NpcSystem
from utils.npc_embeds import (
    build_npc_statblock_embed,
    build_encounter_embed,
    build_add_result_embed,
)
from utils.tags import format_tag
from utils.dice import roll_expression, roll_all_dice_in_text, DiceResult
from utils.targeting import parse_target_flag, resolve_target, TargetResult

MAX_ATTACHMENT_SIZE = 2 * 1024 * 1024
MAX_FILES           = 10

# ── Colour helpers ────────────────────────────────────────────────────────────

DAMAGE_COLORS = {
    "kinetic":   0x9E9E9E,
    "energy":    0x2196F3,
    "explosive": 0xE8871A,
    "burn":      0xFF5722,
    "heat":      0xFF9800,
}
ROLE_COLORS = {
    "controller": 0x9C27B0,
    "striker":    0xCF2020,
    "artillery":  0xE8871A,
    "defender":   0x2196F3,
    "support":    0x4CAF50,
}
DEFAULT_COLOR = 0x455A64

def _weapon_color(w: NpcWeapon) -> int:
    if w.damage:
        return DAMAGE_COLORS.get(w.damage[0]["type"].lower(), DEFAULT_COLOR)
    return DEFAULT_COLOR

def _enemy_color(enemy: NpcEnemy) -> int:
    return ROLE_COLORS.get(enemy.role.lower(), DEFAULT_COLOR)


# ── NPC attack roll dataclass ─────────────────────────────────────────────────

class NpcAttackResult:
    """1d20 + attack_bonus ± accuracy dice."""
    __slots__ = ("d20", "attack_bonus", "accuracy_dice", "acc_rolls",
                 "acc_kept", "acc_applied", "total", "crit")

    def __init__(self, attack_bonus: int, accuracy_dice: int):
        self.d20          = random.randint(1, 20)
        self.attack_bonus = attack_bonus
        self.accuracy_dice = accuracy_dice

        if accuracy_dice > 0:
            self.acc_rolls   = [random.randint(1, 6) for _ in range(accuracy_dice)]
            self.acc_kept    = max(self.acc_rolls)
            self.acc_applied = self.acc_kept
        else:
            self.acc_rolls   = []
            self.acc_kept    = 0
            self.acc_applied = 0

        self.total = self.d20 + self.attack_bonus + self.acc_applied
        self.crit  = self.total >= 20


# ── Roll NPC damage ───────────────────────────────────────────────────────────

def _roll_npc_damage(
    weapon: NpcWeapon,
    crit: bool,
) -> list[tuple[dict, DiceResult, DiceResult | None]]:
    """
    Returns list of (damage_dict, primary_roll, crit_roll_or_None).
    On crit, both rolls are provided; caller picks the higher total per source.
    """
    results = []
    for d in weapon.damage:
        val = str(d.get("val", "0"))
        dtype = d.get("type", "?")
        r1 = roll_expression(val, label=dtype)
        r2 = roll_expression(val, label=dtype) if crit else None
        results.append((d, r1, r2))
    return results


def _progress_bar(current: int, maximum: int, length: int = 10) -> str:
    if maximum <= 0:
        return "█" * length
    filled = round(length * max(0, current) / maximum)
    return "█" * max(0, min(filled, length)) + "░" * max(0, length - filled)


# ── Embed builders ────────────────────────────────────────────────────────────

def _build_weapon_embed(
    enemy: NpcEnemy,
    weapon: NpcWeapon,
    attack: NpcAttackResult,
    dmg_results: list[tuple[dict, DiceResult, DiceResult | None]],
    target: "TargetResult | None" = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"⚔️ {enemy.display_name} — {weapon.name}",
        color=_weapon_color(weapon),
    )
    embed.description = (
        f"**{weapon.weapon_type}**  ·  Range: {weapon.range_str}  "
        f"·  *{enemy.tag} {enemy.role.title()} T{enemy.tier}*"
    )

    # ── Target preview ────────────────────────────────────────────────────────
    if target:
        if target.kind == "player":
            embed.add_field(
                name=f"🎯 Target: {target.label}",
                value="Player — HP tracked on their sheet.",
                inline=False,
            )
        else:
            bar = _progress_bar(target.npc_hp, target.npc_max_hp)
            embed.add_field(
                name=f"🎯 Target: {target.label}",
                value=f"❤️ `{bar}` {target.npc_hp}/{target.npc_max_hp} HP",
                inline=False,
            )

    # ── Attack roll ───────────────────────────────────────────────────────────
    atk_lines = [f"🎲 **{attack.d20}** (d20) + **{attack.attack_bonus}** (Atk Bonus)"]
    if attack.acc_rolls:
        dice_str = ", ".join(str(r) for r in attack.acc_rolls)
        atk_lines.append(
            f"🟢 Accuracy ({attack.accuracy_dice}d6: [{dice_str}] → kept **{attack.acc_kept}**) → +{attack.acc_applied}"
        )
    crit_str = "  🎯 **CRITICAL HIT!**" if attack.crit else ""
    atk_lines.append(f"**Attack Total: {attack.total}**{crit_str}")
    embed.add_field(name="🎯 Attack Roll", value="\n".join(atk_lines), inline=False)

    # ── Damage ────────────────────────────────────────────────────────────────
    if dmg_results:
        dmg_lines = []
        if attack.crit:
            for d, r1, r2 in dmg_results:
                winner = r1 if (r2 is None or r1.total >= r2.total) else r2
                loser  = r2 if winner is r1 else r1

                def _fmt(r: DiceResult) -> str:
                    rolls_str = " + ".join(str(x) for x in r.rolls) if r.rolls else str(r.modifier)
                    mod_str = f" + {r.modifier}" if r.modifier and r.rolls else ""
                    return f"[{rolls_str}]{mod_str} = {r.total}"

                dmg_lines.append(
                    f"**{winner.label}**: "
                    f"Roll 1: {_fmt(winner)} ✅  |  Roll 2: {_fmt(loser)}\n"
                    f"  **Kept: {winner.total}**"
                )
            embed.add_field(
                name="💥 Damage (CRIT — doubled dice, keep highest)",
                value="\n".join(dmg_lines),
                inline=False,
            )
        else:
            for d, r1, _ in dmg_results:
                rolls_str = " + ".join(str(x) for x in r1.rolls) if r1.rolls else str(r1.modifier)
                mod_str = f" + {r1.modifier}" if r1.modifier and r1.rolls else ""
                dmg_lines.append(f"**{r1.label}**: [{rolls_str}]{mod_str} = **{r1.total}**")
            embed.add_field(name="💥 Damage", value="\n".join(dmg_lines), inline=False)

    # ── On Hit / Effect ───────────────────────────────────────────────────────
    if weapon.on_hit:
        embed.add_field(name="✅ On Hit", value=weapon.on_hit[:500], inline=False)
    if weapon.effect:
        embed.add_field(name="📋 Effect", value=weapon.effect[:500], inline=False)

    # ── Tags ──────────────────────────────────────────────────────────────────
    if weapon.tags:
        embed.add_field(
            name="🏷️ Tags",
            value=", ".join(format_tag(t) for t in weapon.tags),
            inline=True,
        )

    # ── NPC heat bar preview (before button) ─────────────────────────────────
    s = enemy.stats
    heat_self = weapon.heat_self()
    if heat_self:
        bar = _progress_bar(s.current_heat, s.heatcap)
        embed.add_field(
            name="🌡️ Self Heat",
            value=f"**+{heat_self}** self heat  ·  Current: `{bar}` {s.current_heat}/{s.heatcap}",
            inline=False,
        )

    embed.set_footer(
        text=f"{enemy.display_name}"
             + (f"  →  {target.label}" if target else "")
             + "  ·  Use the button below to apply"
    )
    return embed


def _build_tech_embed(
    enemy: NpcEnemy,
    system: NpcSystem,
    attack: NpcAttackResult,
    annotated_text: str,
    target: "TargetResult | None" = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"💻 {enemy.display_name} — {system.name}",
        color=0x7E57C2,
    )
    embed.description = (
        f"**{system.tech_type or 'Quick'} Tech**  ·  "
        f"*{enemy.tag} {enemy.role.title()} T{enemy.tier}*"
    )

    # ── Target preview ────────────────────────────────────────────────────────
    if target:
        if target.kind == "player":
            embed.add_field(
                name=f"🎯 Target: {target.label}",
                value="Player — HP tracked on their sheet.",
                inline=False,
            )
        else:
            bar = _progress_bar(target.npc_hp, target.npc_max_hp)
            embed.add_field(
                name=f"🎯 Target: {target.label}",
                value=f"❤️ `{bar}` {target.npc_hp}/{target.npc_max_hp} HP",
                inline=False,
            )

    # Attack roll
    atk_lines = [f"🎲 **{attack.d20}** (d20) + **{attack.attack_bonus}** (Tech Atk)"]
    if attack.acc_rolls:
        dice_str = ", ".join(str(r) for r in attack.acc_rolls)
        atk_lines.append(
            f"🟢 Accuracy ({attack.accuracy_dice}d6: [{dice_str}] → kept **{attack.acc_kept}**) → +{attack.acc_applied}"
        )
    crit_str = "  🎯 **CRITICAL!**" if attack.crit else ""
    atk_lines.append(f"**Tech Attack Total: {attack.total}**{crit_str}")
    embed.add_field(name="💻 Tech Attack", value="\n".join(atk_lines), inline=False)

    if annotated_text:
        embed.add_field(name="📋 Effect", value=annotated_text[:1000], inline=False)

    if system.tags:
        embed.add_field(
            name="🏷️ Tags",
            value=", ".join(format_tag(t) for t in system.tags),
            inline=True,
        )

    embed.set_footer(
        text=f"{enemy.display_name}"
             + (f"  →  {target.label}" if target else "")
    )
    return embed


def _build_system_embed(
    enemy: NpcEnemy,
    system: NpcSystem,
    annotated_text: str,
) -> discord.Embed:
    type_icons = {
        "System":   "🔧",
        "Trait":    "🧬",
        "Reaction": "↩️",
    }
    icon = type_icons.get(system.system_type, "🔧")
    embed = discord.Embed(
        title=f"{icon} {enemy.display_name} — {system.name}",
        color=_enemy_color(enemy),
    )
    embed.description = (
        f"**{system.system_type}**  ·  "
        f"*{enemy.tag} {enemy.role.title()} T{enemy.tier}*"
    )

    if annotated_text:
        embed.add_field(name="📋 Effect", value=annotated_text[:1000], inline=False)

    if system.tags:
        embed.add_field(
            name="🏷️ Tags",
            value=", ".join(format_tag(t) for t in system.tags),
            inline=True,
        )

    embed.set_footer(text=f"{enemy.display_name}")
    return embed


# ── Interactive Views ─────────────────────────────────────────────────────────

class FeaturePickerView(discord.ui.View):
    """
    Shows buttons for each feature on the NPC.
    Weapons & Techs in one colour, Systems/Traits/Reactions in another.
    Up to 20 features shown (4 rows × 5), row 4 is cancel.
    """
    _TYPE_STYLE = {
        "Weapon":   discord.ButtonStyle.danger,
        "Tech":     discord.ButtonStyle.primary,
        "System":   discord.ButtonStyle.secondary,
        "Trait":    discord.ButtonStyle.secondary,
        "Reaction": discord.ButtonStyle.secondary,
    }
    _TYPE_ICON = {
        "Weapon":   "⚔️",
        "Tech":     "💻",
        "System":   "🔧",
        "Trait":    "🧬",
        "Reaction": "↩️",
    }

    def __init__(self, enemy: NpcEnemy, author_id: int):
        super().__init__(timeout=45)
        self.enemy     = enemy
        self.author_id = author_id
        self.chosen: NpcWeapon | NpcSystem | None = None

        all_features: list[NpcWeapon | NpcSystem] = list(enemy.weapons) + list(enemy.systems)

        for i, feat in enumerate(all_features[:20]):
            ftype = feat.weapon_type if isinstance(feat, NpcWeapon) else feat.system_type
            icon  = self._TYPE_ICON.get(
                "Weapon" if isinstance(feat, NpcWeapon) else feat.system_type, "▶️"
            )
            style = self._TYPE_STYLE.get(
                "Weapon" if isinstance(feat, NpcWeapon) else feat.system_type,
                discord.ButtonStyle.secondary,
            )
            btn = discord.ui.Button(
                label=f"{icon} {feat.name}"[:80],
                style=style,
                row=min(i // 5, 3),
            )
            btn.callback = self._make_callback(feat)
            self.add_item(btn)

        cancel = discord.ui.Button(
            label="✖ Cancel",
            style=discord.ButtonStyle.secondary,
            row=4,
        )
        cancel.callback = self._cancel
        self.add_item(cancel)

    def _make_callback(self, feat: NpcWeapon | NpcSystem):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.author_id:
                await interaction.response.send_message("Not your choice.", ephemeral=True)
                return
            self.chosen = feat
            await interaction.response.defer()
            self.stop()
        return callback

    async def _cancel(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Not your choice.", ephemeral=True)
            return
        await interaction.response.defer()
        self.stop()


class NpcActionView(discord.ui.View):
    """
    Buttons shown after an NPC weapon/tech action.

    Row 0  [Apply HP dmg → target]   (only when target is set)
    Row 1  [🌡️ Apply self-heat to NPC]

    When a player target is provided the HP button writes to that player's
    mech HP in utils/storage.  When no target is provided the button falls
    back to a manual-tracking ephemeral.
    """

    def __init__(
        self,
        guild_id:    int,
        channel_id:  int,
        enemy:       NpcEnemy,
        self_heat:   int,
        hp_damage:   int,
        embed:       discord.Embed,
        target:      "TargetResult | None" = None,
    ):
        super().__init__(timeout=120)
        self.guild_id   = guild_id
        self.channel_id = channel_id
        self.enemy      = enemy
        self.self_heat  = self_heat
        self.hp_damage  = hp_damage
        self.embed      = embed
        self.target     = target

        # ── Row 0: target HP button ───────────────────────────────────────────
        if hp_damage <= 0:
            self.apply_target_hp.disabled = True
            self.apply_target_hp.label    = "No HP damage"
        else:
            tname = target.label if target else "target"
            self.apply_target_hp.label = f"💢 Apply {hp_damage} dmg → {tname}"

        if not target:
            # No target set — button will send a manual-track ephemeral
            pass

        # ── Row 1: self-heat button ───────────────────────────────────────────
        if self_heat <= 0:
            self.apply_self_heat.disabled = True
            self.apply_self_heat.label    = "No self-heat"
        else:
            self.apply_self_heat.label = f"🌡️ +{self_heat} Heat → {enemy.display_name}"

    # ── helpers ───────────────────────────────────────────────────────────────

    def _patch_field(self, name: str, value: str) -> None:
        if self.embed is None:
            return
        for i, f in enumerate(self.embed.fields):
            if f.name == name:
                self.embed.set_field_at(i, name=name, value=value, inline=False)
                return
        self.embed.add_field(name=name, value=value, inline=False)

    # ── Row 0 — target HP ────────────────────────────────────────────────────

    @discord.ui.button(style=discord.ButtonStyle.danger, row=0)
    async def apply_target_hp(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if self.target is None:
            await interaction.response.send_message(
                f"📊 Deal **{self.hp_damage} HP damage** to your target manually.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        if self.target.kind == "player":
            # Write to the player's mech HP in the characters DB
            import utils.storage as _storage
            import json as _json
            from utils.lancer_checks import roll_structure_check, attach_cascade

            uid  = self.target.player_user_id
            raw  = _storage.load_raw(self.guild_id, uid)
            char = _storage.load(self.guild_id, uid)

            if raw is None or char is None or char.active_mech is None:
                await interaction.followup.send(
                    f"⚠️ Could not find a character for <@{uid}>.",
                    ephemeral=True,
                )
                return

            data     = _json.loads(raw)
            ms       = char.active_mech.stats
            cur      = data["data"]["mechs"][0]["stats"]["current"]
            old_hp   = cur.get("hp", ms.hp)
            new_hp   = max(0, old_hp - self.hp_damage)
            overflow = max(0, -new_hp)

            structure_result = None
            if new_hp <= 0 and cur.get("structure", ms.structure) > 0:
                structure_result = roll_structure_check(
                    structure_before=cur.get("structure", ms.structure),
                    max_structure=ms.structure,
                    hp_overflow=overflow,
                )
                attach_cascade(structure_result, char.active_mech)
                new_structure    = max(0, structure_result.structure_after)
                cur["structure"] = new_structure
                cur["hp"]        = max(0, ms.hp - overflow) if new_structure > 0 else 0
            else:
                cur["hp"] = new_hp

            _storage.save_raw(self.guild_id, uid, char.pilot.callsign, _json.dumps(data))

            final_hp = cur["hp"]
            bar      = _progress_bar(final_hp, ms.hp)
            field_val = f"❤️ `{bar}` **{old_hp} → {final_hp}/{ms.hp}**"
            if structure_result:
                field_val += (
                    f"\n⚠️ **Structure Check triggered!** "
                    f"({structure_result.result_name})"
                )

            # Post structure embed if needed
            if structure_result:
                from cogs.encounter import _build_structure_embed  # same module
                await interaction.followup.send(
                    embed=_build_structure_embed(structure_result, char.active_mech)
                )

        else:
            # NPC-on-NPC damage (rare but possible)
            old, new, maxhp, killed = _npc_apply_hp(
                self.guild_id, self.channel_id,
                self.target.npc_slug, self.hp_damage,
            )
            bar       = _progress_bar(new, maxhp)
            status    = "💀 **DESTROYED**" if killed else f"**{old} → {new}/{maxhp}**"
            field_val = f"❤️ `{bar}` {status}"

        button.disabled = True
        button.label    = f"✅ {self.hp_damage} dmg applied"
        button.style    = discord.ButtonStyle.success
        self._patch_field(f"💢 {self.target.label} HP", field_val)
        await interaction.edit_original_response(embed=self.embed, view=self)

    # ── Row 1 — self-heat on NPC ─────────────────────────────────────────────

    @discord.ui.button(style=discord.ButtonStyle.primary, row=1)
    async def apply_self_heat(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        enemy = npc_storage.get_enemy(self.guild_id, self.channel_id, self.enemy.slug)
        if enemy is None:
            await interaction.response.send_message("❌ Enemy not found.", ephemeral=True)
            return

        s            = enemy.stats
        old_heat     = s.current_heat
        new_heat_raw = old_heat + self.self_heat
        overloaded   = new_heat_raw > s.heatcap and s.heatcap > 0
        new_heat     = max(0, min(s.heatcap, new_heat_raw))

        npc_storage.update_enemy_vitals(
            self.guild_id, self.channel_id, enemy.slug, heat=new_heat
        )

        button.disabled = True
        button.label    = f"✅ +{self.self_heat} Heat Applied"
        button.style    = discord.ButtonStyle.success

        bar          = _progress_bar(new_heat, s.heatcap)
        dz           = "  🌡️ **DANGER ZONE!**" if new_heat >= s.heatcap // 2 else ""
        overheat_str = (
            "\n☢️ **OVERHEATED** — NPC takes 2 AP Energy, Impaired/Slowed."
            if overloaded else ""
        )
        self._patch_field(
            "🌡️ Self Heat",
            f"**+{self.self_heat}** applied  ·  `{bar}` **{new_heat}/{s.heatcap}**{dz}{overheat_str}",
        )
        await interaction.response.edit_message(embed=self.embed, view=self)


# ── NPC-on-NPC HP helper ──────────────────────────────────────────────────────

def _npc_apply_hp(
    guild_id: int, channel_id: int,
    slug: str, damage: int,
) -> tuple[int, int, int, bool]:
    """Subtract damage from an NPC. Returns (old_hp, new_hp, max_hp, killed)."""
    enemy = npc_storage.get_enemy(guild_id, channel_id, slug)
    if enemy is None:
        return 0, 0, 0, False
    s      = enemy.stats
    old_hp = s.current_hp
    new_hp = max(0, old_hp - damage)
    killed = new_hp == 0 and s.current_structure <= 1
    npc_storage.update_enemy_vitals(
        guild_id, channel_id, slug,
        hp=new_hp, is_dead=killed if killed else None,
    )
    return old_hp, new_hp, s.hp, killed


# ── _build_structure_embed (re-exported for use by NpcActionView) ─────────────

def _build_structure_embed(result, mech) -> discord.Embed:
    """Thin wrapper — the real impl lives in use.py but we need it here too."""
    color = 0x1A1A1A if result.destroyed else (0xFF5252 if result.lowest == 1 else 0xFF9800)
    embed = discord.Embed(
        title=f"🦾 Structure Check — {result.result_name}",
        color=color,
    )
    if result.dice_rolled:
        dice_str = "  ".join(
            f"**{d}**" if d == result.lowest else str(d)
            for d in result.dice_rolled
        )
        embed.add_field(
            name=f"🎲 Rolled {len(result.dice_rolled)}d6 (lowest is worst)",
            value=dice_str + f"\n→ Kept: **{result.lowest}**",
            inline=False,
        )
    struct_pips = (
        "█" * result.structure_after
        + "░" * (result.structure_before - result.structure_after)
    )
    if result.structure_before > 0:
        embed.add_field(
            name="🛡️ Structure",
            value=f"`{struct_pips}` {result.structure_after}/{result.structure_before}",
            inline=True,
        )
    if result.hp_overflow:
        embed.add_field(
            name="💢 HP Overflow",
            value=f"**{result.hp_overflow}** damage carries to next structure",
            inline=True,
        )
    embed.add_field(name="📋 Result", value=result.result_detail, inline=False)
    if getattr(result, "nhp_present", False):
        if result.cascade_triggered:
            embed.add_field(
                name="NHP Cascade",
                value=f"d20: **{result.cascade_roll}** = 1  →  ⚠️ **CASCADE!**",
                inline=False,
            )
        else:
            embed.add_field(
                name="NHP Cascade",
                value=f"d20: **{result.cascade_roll}**  →  ✅ No cascade.",
                inline=False,
            )
    return embed


# ── Fuzzy feature search ──────────────────────────────────────────────────────

def _fuzzy_feature(
    query: str,
    enemy: NpcEnemy,
) -> list[NpcWeapon | NpcSystem]:
    """Case-insensitive word-by-word match across weapons + systems."""
    q     = query.strip().lower()
    words = q.split()
    all_f: list[NpcWeapon | NpcSystem] = list(enemy.weapons) + list(enemy.systems)

    multi = [f for f in all_f if all(w in f.name.lower() for w in words)]
    if multi:
        return multi
    return [f for f in all_f if q in f.name.lower()]


# ── Helper: resolve enemy ─────────────────────────────────────────────────────

def _resolve(guild_id: int, channel_id: int, query: str) -> NpcEnemy | None:
    return npc_storage.resolve_enemy_slug(guild_id, channel_id, query)


def _require_enemy(
    guild_id: int,
    channel_id: int,
    query: str,
) -> tuple[NpcEnemy | None, str | None]:
    enemy = _resolve(guild_id, channel_id, query)
    if enemy is None:
        enemies = npc_storage.list_enemies(guild_id, channel_id)
        if not enemies:
            return None, "❌ No enemies in this encounter. Use `!npc add` to add some."
        names = ", ".join(f"**{e.display_name}**" for e in enemies)
        return None, f"❌ No enemy matching **\"{query}\"**. Available: {names}"
    return enemy, None


# ── Confirmation view ─────────────────────────────────────────────────────────

class ConfirmView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=30)
        self.confirmed  = False
        self.author_id  = author_id

    @discord.ui.button(label="✅ Yes, clear everything", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Not your call.", ephemeral=True)
            return
        self.confirmed = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Not your call.", ephemeral=True)
            return
        await interaction.response.defer()
        self.stop()


# ── Activate selection view ───────────────────────────────────────────────────

class ActivateView(discord.ui.View):
    def __init__(self, enemies: list[NpcEnemy], author_id: int):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.selected: set[str] = {e.slug for e in enemies if e.is_active}
        self.enemies   = enemies
        self.confirmed = False
        self._build_buttons()

    def _build_buttons(self):
        self.clear_items()
        for i, e in enumerate(self.enemies[:20]):
            row    = i // 5
            active = e.slug in self.selected
            style  = discord.ButtonStyle.success if active else discord.ButtonStyle.secondary
            label  = f"{'⚡ ' if active else ''}{e.display_name}"
            btn    = discord.ui.Button(label=label[:80], style=style, row=row)
            btn.callback = self._make_toggle(e.slug)
            self.add_item(btn)

        confirm_btn = discord.ui.Button(
            label="✅ Confirm", style=discord.ButtonStyle.primary, row=4
        )
        confirm_btn.callback = self._confirm_callback
        self.add_item(confirm_btn)

        clear_btn = discord.ui.Button(
            label="✖ Clear All", style=discord.ButtonStyle.danger, row=4
        )
        clear_btn.callback = self._clear_callback
        self.add_item(clear_btn)

    def _make_toggle(self, slug: str):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.author_id:
                await interaction.response.send_message("Not your selection.", ephemeral=True)
                return
            self.selected.discard(slug) if slug in self.selected else self.selected.add(slug)
            self._build_buttons()
            await interaction.response.edit_message(view=self)
        return callback

    async def _confirm_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Not your selection.", ephemeral=True)
            return
        self.confirmed = True
        await interaction.response.defer()
        self.stop()

    async def _clear_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Not your selection.", ephemeral=True)
            return
        self.selected.clear()
        self._build_buttons()
        await interaction.response.edit_message(view=self)


# ── Disambiguation view ───────────────────────────────────────────────────────

class FeatureDisambiguateView(discord.ui.View):
    """Shown when a query matches more than one feature."""
    _TYPE_STYLE = {
        "Weapon":   discord.ButtonStyle.danger,
        "Tech":     discord.ButtonStyle.primary,
        "System":   discord.ButtonStyle.secondary,
        "Trait":    discord.ButtonStyle.secondary,
        "Reaction": discord.ButtonStyle.secondary,
    }
    _TYPE_ICON = {
        "Weapon":   "⚔️",
        "Tech":     "💻",
        "System":   "🔧",
        "Trait":    "🧬",
        "Reaction": "↩️",
    }

    def __init__(self, matches: list[NpcWeapon | NpcSystem], author_id: int):
        super().__init__(timeout=30)
        self.author_id = author_id
        self.chosen: NpcWeapon | NpcSystem | None = None

        for i, feat in enumerate(matches[:5]):
            is_weapon = isinstance(feat, NpcWeapon)
            ftype_key = "Weapon" if is_weapon else feat.system_type
            icon  = self._TYPE_ICON.get(ftype_key, "▶️")
            style = self._TYPE_STYLE.get(ftype_key, discord.ButtonStyle.secondary)
            btn   = discord.ui.Button(label=f"{icon} {feat.name}"[:80], style=style, row=i)
            btn.callback = self._make_cb(feat)
            self.add_item(btn)

    def _make_cb(self, feat: NpcWeapon | NpcSystem):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.author_id:
                await interaction.response.send_message("Not your choice.", ephemeral=True)
                return
            self.chosen = feat
            await interaction.response.defer()
            self.stop()
        return callback


# ══════════════════════════════════════════════════════════════════════════════
# Cog
# ══════════════════════════════════════════════════════════════════════════════

class EncounterCog(commands.Cog, name="Encounter"):
    """GM commands for managing NPC encounters."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── !npc (root group) ─────────────────────────────────────────────────────

    @commands.group(
        name="npc",
        aliases=["encounter", "enc"],
        invoke_without_command=True,
    )
    async def npc(self, ctx: commands.Context):
        """Encounter management.  Run `!npc` for a quick guide."""
        embed = discord.Embed(
            title="⚔️ Encounter Commands",
            description="Manage NPC enemies for your Lancer session.",
            color=0xCF2020,
        )
        embed.add_field(
            name="📥 Adding enemies",
            value=(
                "`!npc add`              — attach 1+ NPC JSON files\n"
                "`!npc add -qty 3`       — 3 copies of attached NPC (→ A, B, C)\n"
                "Multiple files can be attached at once."
            ),
            inline=False,
        )
        embed.add_field(
            name="📋 Viewing",
            value=(
                "`!npc list`             — encounter roster overview\n"
                "`!npc show <name>`      — full statblock"
            ),
            inline=False,
        )
        embed.add_field(
            name="⚡ Activating enemies",
            value=(
                "`!npc activate`         — interactive selector\n"
                "`!npc activate <name…>` — activate by name directly\n"
                "`!npc deactivate`       — clear active markers"
            ),
            inline=False,
        )
        embed.add_field(
            name="🎲 NPC Actions  (!npca)",
            value=(
                "Uses the **active** enemy automatically — activate first with `!npc activate`.\n"
                "`!npca`                              — pick from active enemy's actions\n"
                "`!npca graviton lance`               — fire that weapon (active enemy)\n"
                "`!npca drag down`                    — use that tech (active enemy)\n"
                "`!npca mobile printer`               — show system effect (active enemy)\n"
                "`!npca <enemy> <feature>`            — explicit target (reactions / 2+ active)\n"
                "`!npca graviton lance -t @Player`    — target player; button applies HP dmg\n"
                "`!npca drag down -t @Player`         — tech attack on player\n"
                "Accuracy: add `acc 2` or `a2` at the end"
            ),
            inline=False,
        )
        embed.add_field(
            name="🩸 Tracking vitals",
            value=(
                "`!npc hp <name> +5`     — add HP\n"
                "`!npc hp <name> -3`     — remove HP\n"
                "`!npc heat <name> +4`   — add heat\n"
                "`!npc fr <name>`        — full repair"
            ),
            inline=False,
        )
        embed.add_field(
            name="🗑️ Removing",
            value=(
                "`!npc kill <name>`      — mark destroyed\n"
                "`!npc clear`            — wipe the whole encounter"
            ),
            inline=False,
        )
        await ctx.reply(embed=embed, mention_author=False)

    # ── !npc add ──────────────────────────────────────────────────────────────

    @npc.command(name="add", aliases=["a", "import"])
    async def npc_add(self, ctx: commands.Context, *args: str):
        """
        Import NPC JSON file(s) into the encounter.
        Use `-qty N` to add N copies (gets A, B, C… suffixes).
        Attach multiple files to add multiple different NPCs at once.
        """
        if not ctx.message.attachments:
            await ctx.reply(
                "❌ No file attached.\n"
                "Attach NPC JSON file(s) from comp/con, then run `!npc add`.\n"
                "Use `-qty N` for multiple copies: `!npc add -qty 2`",
                mention_author=False,
            )
            return

        qty  = 1
        text = " ".join(args).lower()
        qty_m = re.search(r"-qty\s+(\d+)", text)
        if qty_m:
            qty = max(1, min(int(qty_m.group(1)), 26))

        attachments = ctx.message.attachments[:MAX_FILES]
        errors: list[str] = []
        raw_jsons: list[str] = []

        async with ctx.typing():
            for att in attachments:
                if not att.filename.endswith(".json"):
                    errors.append(f"⚠️ `{att.filename}` — not a .json file, skipped.")
                    continue
                if att.size > MAX_ATTACHMENT_SIZE:
                    errors.append(f"⚠️ `{att.filename}` — too large, skipped.")
                    continue
                raw = await att.read()
                try:
                    from utils.npc_parser import parse_npc_json
                    parse_npc_json(raw)   # validate
                    raw_jsons.append(raw.decode("utf-8"))
                except ValueError as e:
                    errors.append(f"⚠️ `{att.filename}` — {e}")

        if not raw_jsons:
            msg = "❌ No valid NPC files could be imported."
            if errors:
                msg += "\n" + "\n".join(errors)
            await ctx.reply(msg, mention_author=False)
            return

        async with ctx.typing():
            added = npc_storage.add_enemies_batch(
                ctx.guild.id, ctx.channel.id,
                raw_jsons, [qty] * len(raw_jsons),
            )

        embed = build_add_result_embed(added)
        if errors:
            embed.add_field(name="⚠️ Warnings", value="\n".join(errors), inline=False)
        embed.set_author(
            name=f"Added by {ctx.author.display_name}",
            icon_url=ctx.author.display_avatar.url,
        )
        await ctx.reply(embed=embed, mention_author=False)

    # ── !npc list ─────────────────────────────────────────────────────────────

    @npc.command(name="list", aliases=["ls", "roster"])
    async def npc_list(self, ctx: commands.Context):
        """Show the current encounter roster."""
        enemies = npc_storage.list_enemies(ctx.guild.id, ctx.channel.id, include_dead=True)
        if not enemies:
            await ctx.reply("No enemies in this encounter. Use `!npc add` to add some.", mention_author=False)
            return
        embed = build_encounter_embed(enemies)
        embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
        await ctx.reply(embed=embed, mention_author=False)

    # ── !npc show ─────────────────────────────────────────────────────────────

    @npc.command(name="show", aliases=["s", "stat", "statblock", "info"])
    async def npc_show(self, ctx: commands.Context, *, name: str = ""):
        """Show the full statblock for one enemy.  Usage: !npc show barricade"""
        if not name:
            await ctx.reply("Usage: `!npc show <name>`", mention_author=False)
            return
        enemy, err = _require_enemy(ctx.guild.id, ctx.channel.id, name)
        if err:
            await ctx.reply(err, mention_author=False)
            return
        embed = build_npc_statblock_embed(enemy)
        embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
        await ctx.reply(embed=embed, mention_author=False)

    # ── !npc activate ─────────────────────────────────────────────────────────

    @npc.command(name="activate", aliases=["act", "active"])
    async def npc_activate(self, ctx: commands.Context, *, names: str = ""):
        """
        Mark enemies as active for their turn.
        No arguments → interactive selector.
        With names (comma-separated) → activate directly.
        """
        enemies = npc_storage.list_enemies(ctx.guild.id, ctx.channel.id)
        if not enemies:
            await ctx.reply("No enemies in this encounter. Use `!npc add` first.", mention_author=False)
            return

        if names.strip():
            raw_names = [n.strip() for n in re.split(r",\s*|\s{2,}", names) if n.strip()]
            if len(raw_names) == 1:
                raw_names = [names.strip()]

            resolved: list[str] = []
            not_found: list[str] = []
            for qname in raw_names:
                e = _resolve(ctx.guild.id, ctx.channel.id, qname)
                if e:
                    resolved.append(e.slug)
                else:
                    not_found.append(qname)

            if not resolved:
                all_names = ", ".join(f"**{e.display_name}**" for e in enemies)
                await ctx.reply(f"❌ None matched. Available: {all_names}", mention_author=False)
                return

            npc_storage.set_active_enemies(ctx.guild.id, ctx.channel.id, resolved)
            updated = npc_storage.list_enemies(ctx.guild.id, ctx.channel.id)
            active_names = ", ".join(f"**{e.display_name}**" for e in updated if e.is_active)
            msg = f"⚡ Activated: {active_names}"
            if not_found:
                msg += f"\n⚠️ Not found: {', '.join(not_found)}"
            embed = build_encounter_embed(updated, title="⚡ Activation Updated")
            await ctx.reply(msg, embed=embed, mention_author=False)
            return

        # Interactive selector
        view = ActivateView(enemies, ctx.author.id)
        embed = build_encounter_embed(enemies, title="⚡ Select Active Enemies")
        embed.set_footer(text="Toggle enemies below, then click ✅ Confirm")
        msg = await ctx.reply(embed=embed, view=view, mention_author=False)
        await view.wait()

        if not view.confirmed:
            await msg.edit(content="⏱️ Timed out.", embed=None, view=None)
            return

        npc_storage.set_active_enemies(ctx.guild.id, ctx.channel.id, list(view.selected))
        updated     = npc_storage.list_enemies(ctx.guild.id, ctx.channel.id)
        active_names = ", ".join(f"**{e.display_name}**" for e in updated if e.is_active) or "*(none)*"
        await msg.edit(
            content=f"⚡ Active: {active_names}",
            embed=build_encounter_embed(updated, title="⚡ Activation Updated"),
            view=None,
        )

    # ── !npc deactivate ───────────────────────────────────────────────────────

    @npc.command(name="deactivate", aliases=["deact", "endturn"])
    async def npc_deactivate(self, ctx: commands.Context):
        """Clear all active markers (end of NPC turn)."""
        npc_storage.set_active_enemies(ctx.guild.id, ctx.channel.id, [])
        enemies = npc_storage.list_enemies(ctx.guild.id, ctx.channel.id)
        embed   = build_encounter_embed(enemies, title="✅ All enemies deactivated")
        await ctx.reply(embed=embed, mention_author=False)

    # ── !npc hp ───────────────────────────────────────────────────────────────

    @npc.command(name="hp")
    async def npc_hp(self, ctx: commands.Context, *, args: str = ""):
        """
        Adjust/show an enemy's HP.
        Usage: !npc hp <name> [+/-amount or exact]
        Example: !npc hp barricade a -5
        """
        parts = args.strip().rsplit(None, 1)
        if len(parts) == 2 and re.fullmatch(r"[+-]?\d+", parts[1]):
            name_query, amount_str = parts[0], parts[1]
        else:
            name_query, amount_str = args.strip(), None

        if not name_query:
            await ctx.reply("Usage: `!npc hp <name> [amount]`", mention_author=False)
            return

        enemy, err = _require_enemy(ctx.guild.id, ctx.channel.id, name_query)
        if err:
            await ctx.reply(err, mention_author=False)
            return

        s = enemy.stats
        if amount_str is None:
            bar = _progress_bar(s.current_hp, s.hp)
            await ctx.reply(
                f"❤️ **{enemy.display_name}** HP: `{bar}` **{s.current_hp}/{s.hp}**",
                mention_author=False,
            )
            return

        is_relative = amount_str.startswith(("+", "-"))
        delta   = int(amount_str)
        old_hp  = s.current_hp
        new_hp  = max(0, min(s.hp, (old_hp + delta) if is_relative else delta))
        killed  = new_hp == 0 and s.current_structure <= 1

        npc_storage.update_enemy_vitals(
            ctx.guild.id, ctx.channel.id, enemy.slug,
            hp=new_hp, is_dead=killed if killed else None,
        )

        bar    = _progress_bar(new_hp, s.hp)
        change = f"({delta:+})" if is_relative else "(set)"
        msg    = f"❤️ **{enemy.display_name}** HP: `{bar}` **{old_hp}** → **{new_hp}/{s.hp}** {change}"
        if killed:
            msg += "\n💀 **DESTROYED** — HP and Structure both at 0."
        await ctx.reply(msg, mention_author=False)

    # ── !npc heat ─────────────────────────────────────────────────────────────

    @npc.command(name="heat")
    async def npc_heat(self, ctx: commands.Context, *, args: str = ""):
        """
        Adjust/show an enemy's heat.
        Usage: !npc heat <name> [+/-amount or exact]
        """
        parts = args.strip().rsplit(None, 1)
        if len(parts) == 2 and re.fullmatch(r"[+-]?\d+", parts[1]):
            name_query, amount_str = parts[0], parts[1]
        else:
            name_query, amount_str = args.strip(), None

        if not name_query:
            await ctx.reply("Usage: `!npc heat <name> [amount]`", mention_author=False)
            return

        enemy, err = _require_enemy(ctx.guild.id, ctx.channel.id, name_query)
        if err:
            await ctx.reply(err, mention_author=False)
            return

        s = enemy.stats
        if amount_str is None:
            bar = _progress_bar(s.current_heat, s.heatcap)
            dz  = "  🌡️ DANGER ZONE" if s.current_heat >= s.heatcap // 2 else ""
            await ctx.reply(
                f"🌡️ **{enemy.display_name}** Heat: `{bar}` **{s.current_heat}/{s.heatcap}**{dz}",
                mention_author=False,
            )
            return

        is_relative  = amount_str.startswith(("+", "-"))
        delta        = int(amount_str)
        old_heat     = s.current_heat
        new_heat_raw = (old_heat + delta) if is_relative else delta
        overloaded   = new_heat_raw > s.heatcap and s.heatcap > 0
        new_heat     = max(0, min(s.heatcap, new_heat_raw))

        npc_storage.update_enemy_vitals(ctx.guild.id, ctx.channel.id, enemy.slug, heat=new_heat)

        bar    = _progress_bar(new_heat, s.heatcap)
        change = f"({delta:+})" if is_relative else "(set)"
        dz     = "  🌡️ **DANGER ZONE!**" if new_heat >= s.heatcap // 2 else ""
        msg    = f"🌡️ **{enemy.display_name}** Heat: `{bar}` **{old_heat}** → **{new_heat}/{s.heatcap}** {change}{dz}"
        if overloaded:
            msg += "\n☢️ **OVERHEATED** — NPC takes 2 AP Energy, Impaired/Slowed."
        await ctx.reply(msg, mention_author=False)

    # ── !npc kill ─────────────────────────────────────────────────────────────

    @npc.command(name="kill", aliases=["remove", "destroy", "dead"])
    async def npc_kill(self, ctx: commands.Context, *, name: str = ""):
        """Mark an enemy as destroyed."""
        if not name:
            await ctx.reply("Usage: `!npc kill <name>`", mention_author=False)
            return
        enemy, err = _require_enemy(ctx.guild.id, ctx.channel.id, name)
        if err:
            await ctx.reply(err, mention_author=False)
            return
        npc_storage.update_enemy_vitals(ctx.guild.id, ctx.channel.id, enemy.slug, is_dead=True)
        enemies = npc_storage.list_enemies(ctx.guild.id, ctx.channel.id, include_dead=True)
        embed   = build_encounter_embed(enemies, title=f"💀 {enemy.display_name} Destroyed")
        await ctx.reply(embed=embed, mention_author=False)

    # ── !npc fr ───────────────────────────────────────────────────────────────

    @npc.command(name="fr", aliases=["fullrepair", "repair"])
    async def npc_fr(self, ctx: commands.Context, *, name: str = ""):
        """Full repair an enemy."""
        if not name:
            await ctx.reply("Usage: `!npc fr <name>`", mention_author=False)
            return
        enemy, err = _require_enemy(ctx.guild.id, ctx.channel.id, name)
        if err:
            await ctx.reply(err, mention_author=False)
            return
        s = enemy.stats
        npc_storage.update_enemy_vitals(
            ctx.guild.id, ctx.channel.id, enemy.slug,
            hp=s.hp, heat=0, structure=s.structure, stress=s.stress,
            burn=0, overshield=0, is_dead=False,
        )
        await ctx.reply(
            f"🔧 **{enemy.display_name}** fully repaired — "
            f"HP **{s.hp}/{s.hp}**, Heat **0/{s.heatcap}**, Struct **{s.structure}/{s.structure}**.",
            mention_author=False,
        )

    # ── !npc clear ────────────────────────────────────────────────────────────

    @npc.command(name="clear", aliases=["wipe", "reset"])
    async def npc_clear(self, ctx: commands.Context):
        """Wipe the entire encounter (requires confirmation)."""
        enemies = npc_storage.list_enemies(ctx.guild.id, ctx.channel.id, include_dead=True)
        if not enemies:
            await ctx.reply("No encounter to clear.", mention_author=False)
            return
        view = ConfirmView(ctx.author.id)
        await ctx.reply(
            f"⚠️ This will remove all **{len(enemies)}** enemies. Are you sure?",
            view=view, mention_author=False,
        )
        await view.wait()
        if view.confirmed:
            removed = npc_storage.clear_encounter(ctx.guild.id, ctx.channel.id)
            await ctx.send(f"🗑️ Encounter cleared — {removed} enemies removed.")
        else:
            await ctx.send("Cancelled.")

    # ══════════════════════════════════════════════════════════════════════════
    # !npca  —  NPC action command
    # ══════════════════════════════════════════════════════════════════════════

    @commands.command(name="npca", aliases=["npcaction", "na"])
    async def npca(self, ctx: commands.Context, *args: str):
        """
        Use a weapon or system as an NPC.

        Normal usage (active enemy is set via !npc activate):
        ───────────────────────────────────────────────────────
            !npca                        — interactive picker for the active enemy
            !npca graviton lance         — fire that weapon  (active enemy)
            !npca drag down              — tech attack       (active enemy)
            !npca mobile printer         — show system       (active enemy)
            !npca graviton lance acc 2   — +2 accuracy dice

        Explicit targeting (reactions, or when 2+ enemies are active):
        ───────────────────────────────────────────────────────────────
            !npca <enemy> <feature>
            !npca barricade graviton lance
            !npca barricade a drag down acc 1

        Routing priority
        ─────────────────
        1. Strip the acc flag.
        2. If the remaining text matches a feature on ANY active enemy
           → use that enemy (or ask which one if multiple active enemies share the name).
        3. Otherwise fall back to old <enemy> <feature> prefix-split logic
           (lets GMs still target a specific NPC by name for reactions etc.).
        4. No text → interactive picker for the (single) active enemy.
        """
        enemies_alive = npc_storage.list_enemies(ctx.guild.id, ctx.channel.id)
        if not enemies_alive:
            await ctx.reply(
                "❌ No enemies in this encounter. Use `!npc add` to add some.",
                mention_author=False,
            )
            return

        active_enemies = [e for e in enemies_alive if e.is_active]

        # ── Strip -t / --target flag ──────────────────────────────────────────
        text        = " ".join(args)
        text, raw_target = parse_target_flag(text)

        # Resolve target now (needs guild members for display-name lookup)
        target: TargetResult | None = None
        if raw_target:
            guild_members = (
                {m.id: m.display_name for m in ctx.guild.members}
                if ctx.guild else {}
            )
            target = resolve_target(
                ctx.guild.id, ctx.channel.id, raw_target, guild_members
            )
            if target is None:
                await ctx.reply(
                    f"⚠️ Could not resolve target **\"{raw_target}\"**. "
                    "Continuing without a target.",
                    mention_author=False,
                )

        # ── Strip accuracy flag ───────────────────────────────────────────────
        acc_bonus = 0
        acc_m = re.search(r"\b(?:acc(?:uracy)?)\s*(\d+)", text, re.IGNORECASE)
        if not acc_m:
            acc_m = re.search(r"\ba(\d+)\b", text, re.IGNORECASE)
        if acc_m:
            acc_bonus = int(acc_m.group(1))
            text = (text[:acc_m.start()] + text[acc_m.end():]).strip()

        text = text.strip()

        # ══════════════════════════════════════════════════════════════════════
        # CASE A — no text at all
        # ══════════════════════════════════════════════════════════════════════
        if not text:
            if not active_enemies:
                await ctx.reply(
                    "❌ No active enemies. Use `!npc activate <name>` first, "
                    "or run `!npca <enemy> <feature>` to target explicitly.",
                    mention_author=False,
                )
                return

            if len(active_enemies) == 1:
                # Single active enemy → show its picker
                enemy = active_enemies[0]
                await self._show_feature_picker(ctx, enemy, acc_bonus, target)
            else:
                # Multiple active enemies → ask which one to act
                enemy = await self._pick_active_enemy(ctx, active_enemies)
                if enemy is None:
                    return
                await self._show_feature_picker(ctx, enemy, acc_bonus, target)
            return

        # ══════════════════════════════════════════════════════════════════════
        # CASE B — text provided
        # Try it as a feature query against active enemies FIRST.
        # ══════════════════════════════════════════════════════════════════════
        if active_enemies:
            # Collect (enemy, feature) pairs where the feature fuzzy-matches
            hits: list[tuple[NpcEnemy, NpcWeapon | NpcSystem]] = []
            for e in active_enemies:
                for feat in _fuzzy_feature(text, e):
                    hits.append((e, feat))

            if hits:
                # ── Exactly one match across all active enemies ───────────────
                if len(hits) == 1:
                    enemy, feature = hits[0]
                    async with ctx.typing():
                        await self._execute_feature(ctx, enemy, feature, acc_bonus, target)
                    return

                # ── Multiple hits: deduplicate by (enemy.slug, feature.name) ─
                # Could be the same feature on multiple active enemies, or
                # multiple features on the same enemy matching the query.
                seen: set[tuple[str, str]] = set()
                unique_hits: list[tuple[NpcEnemy, NpcWeapon | NpcSystem]] = []
                for e, f in hits:
                    key = (e.slug, f.name)
                    if key not in seen:
                        seen.add(key)
                        unique_hits.append((e, f))

                if len(unique_hits) == 1:
                    enemy, feature = unique_hits[0]
                    async with ctx.typing():
                        await self._execute_feature(ctx, enemy, feature, acc_bonus, target)
                    return

                # Still multiple → disambiguation picker
                chosen = await self._pick_enemy_feature(ctx, unique_hits)
                if chosen is None:
                    return
                enemy, feature = chosen
                async with ctx.typing():
                    await self._execute_feature(ctx, enemy, feature, acc_bonus, target)
                return

        # ══════════════════════════════════════════════════════════════════════
        # CASE C — no active enemy matched; fall back to <enemy> <feature>
        # prefix-split (supports reactions, explicit targeting, inactive NPCs).
        # ══════════════════════════════════════════════════════════════════════
        enemy: NpcEnemy | None = None
        feature_query           = ""

        words = text.split()
        for split in range(len(words), 0, -1):
            candidate_name = " ".join(words[:split])
            candidate_feat = " ".join(words[split:])
            e = npc_storage.resolve_enemy_slug(ctx.guild.id, ctx.channel.id, candidate_name)
            if e:
                enemy         = e
                feature_query = candidate_feat
                break

        if enemy is None:
            # Nothing matched at all — give a useful error
            if active_enemies:
                active_names = ", ".join(f"**{e.display_name}**" for e in active_enemies)
                all_features = ", ".join(
                    f"**{f.name}**"
                    for e in active_enemies
                    for f in list(e.weapons) + list(e.systems)
                )
                await ctx.reply(
                    f"❌ **\"{text}\"** didn't match any feature on the active "
                    f"{'enemy' if len(active_enemies)==1 else 'enemies'} "
                    f"({active_names}), and didn't match any NPC name either.\n"
                    f"Active features: {all_features}",
                    mention_author=False,
                )
            else:
                names = ", ".join(f"**{e.display_name}**" for e in enemies_alive)
                await ctx.reply(
                    f"❌ No active enemies and **\"{text}\"** didn't match any NPC name.\n"
                    f"Activate an enemy with `!npc activate` first, or use "
                    f"`!npca <enemy> <feature>`.\nAvailable enemies: {names}",
                    mention_author=False,
                )
            return

        # Enemy found via prefix-split
        if not feature_query:
            # Just an enemy name, no feature → show picker for that enemy
            await self._show_feature_picker(ctx, enemy, acc_bonus, target)
            return

        matches = _fuzzy_feature(feature_query, enemy)
        if not matches:
            all_names = ", ".join(
                f"**{f.name}**" for f in list(enemy.weapons) + list(enemy.systems)
            )
            await ctx.reply(
                f"❌ No feature matching **\"{feature_query}\"** on {enemy.display_name}.\n"
                f"Available: {all_names}",
                mention_author=False,
            )
            return

        if len(matches) == 1:
            async with ctx.typing():
                await self._execute_feature(ctx, enemy, matches[0], acc_bonus, target)
            return

        # Multiple feature matches on explicit enemy → disambiguate
        pairs = [(enemy, m) for m in matches]
        chosen = await self._pick_enemy_feature(ctx, pairs)
        if chosen is None:
            return
        enemy, feature = chosen
        async with ctx.typing():
            await self._execute_feature(ctx, enemy, feature, acc_bonus, target)

    # ── Internal helpers for npca routing ────────────────────────────────────

    async def _show_feature_picker(
        self,
        ctx: commands.Context,
        enemy: NpcEnemy,
        acc_bonus: int,
        target: "TargetResult | None" = None,
    ) -> None:
        """Post the FeaturePickerView embed for one enemy and execute on selection."""
        # Re-fetch fresh vitals
        fresh = npc_storage.get_enemy(ctx.guild.id, ctx.channel.id, enemy.slug)
        if fresh:
            enemy = fresh

        view  = FeaturePickerView(enemy, ctx.author.id)
        embed = discord.Embed(
            title=f"🎲 {enemy.display_name} — Choose an Action",
            description=(
                f"**{enemy.tag}** {enemy.role.title()} T{enemy.tier}  ·  "
                f"❤️ {enemy.stats.current_hp}/{enemy.stats.hp} HP  "
                f"🌡️ {enemy.stats.current_heat}/{enemy.stats.heatcap} Heat"
            ),
            color=_enemy_color(enemy),
        )

        for group_type, icon, label in [
            ("Weapon",   "⚔️", "Weapons"),
            ("Tech",     "💻", "Tech Actions"),
            ("System",   "🔧", "Systems"),
            ("Trait",    "🧬", "Traits"),
            ("Reaction", "↩️", "Reactions"),
        ]:
            items = (
                list(enemy.weapons) if group_type == "Weapon"
                else [s for s in enemy.systems if s.system_type == group_type]
            )
            if not items:
                continue
            lines = []
            for feat in items:
                if isinstance(feat, NpcWeapon):
                    lines.append(
                        f"**{feat.name}** — {feat.weapon_type}, "
                        f"{feat.damage_str}, Atk +{feat.attack_bonus}"
                    )
                else:
                    snippet = feat.effect[:80] + ("…" if len(feat.effect) > 80 else "")
                    lines.append(f"**{feat.name}** — {snippet}")
            embed.add_field(name=f"{icon} {label}", value="\n".join(lines), inline=False)

        embed.set_footer(text="Select an action below · !npca <feature> to skip this menu")
        msg = await ctx.reply(embed=embed, view=view, mention_author=False)
        await view.wait()

        if view.chosen is None:
            await msg.edit(content="⏱️ Timed out or cancelled.", embed=None, view=None)
            return

        await msg.edit(
            content=f"🎲 **{enemy.display_name}** uses **{view.chosen.name}**…",
            embed=None, view=None,
        )
        await self._execute_feature(ctx, enemy, view.chosen, acc_bonus, target)

    async def _pick_active_enemy(
        self,
        ctx: commands.Context,
        active_enemies: list[NpcEnemy],
    ) -> NpcEnemy | None:
        """
        When multiple enemies are active and the GM runs a bare !npca,
        ask which one is acting with a simple button row.
        """
        class _PickView(discord.ui.View):
            def __init__(self, enemies: list[NpcEnemy], author_id: int):
                super().__init__(timeout=30)
                self.chosen: NpcEnemy | None = None
                self.author_id = author_id
                for i, e in enumerate(enemies[:5]):
                    btn = discord.ui.Button(
                        label=f"⚡ {e.display_name}"[:80],
                        style=discord.ButtonStyle.primary,
                        row=i,
                    )
                    btn.callback = self._make_cb(e)
                    self.add_item(btn)

            def _make_cb(self, e: NpcEnemy):
                async def cb(interaction: discord.Interaction):
                    if interaction.user.id != self.author_id:
                        await interaction.response.send_message("Not your pick.", ephemeral=True)
                        return
                    self.chosen = e
                    await interaction.response.defer()
                    self.stop()
                return cb

        view = _PickView(active_enemies, ctx.author.id)
        names = "  ·  ".join(f"**{e.display_name}**" for e in active_enemies)
        msg   = await ctx.reply(
            f"⚡ Multiple active enemies: {names}\nWhich one is acting?",
            view=view,
            mention_author=False,
        )
        await view.wait()
        await msg.delete()
        return view.chosen

    async def _pick_enemy_feature(
        self,
        ctx: commands.Context,
        pairs: list[tuple[NpcEnemy, NpcWeapon | NpcSystem]],
    ) -> tuple[NpcEnemy, NpcWeapon | NpcSystem] | None:
        """
        Disambiguation picker when the same query matches features on
        multiple active enemies, or multiple features on one enemy.
        Each button label is '<EnemyName> — <FeatureName>'.
        """
        _TYPE_STYLE = {
            "Weapon":   discord.ButtonStyle.danger,
            "Tech":     discord.ButtonStyle.primary,
            "System":   discord.ButtonStyle.secondary,
            "Trait":    discord.ButtonStyle.secondary,
            "Reaction": discord.ButtonStyle.secondary,
        }
        _TYPE_ICON = {
            "Weapon": "⚔️", "Tech": "💻",
            "System": "🔧", "Trait": "🧬", "Reaction": "↩️",
        }

        class _PairView(discord.ui.View):
            def __init__(
                self,
                pairs: list[tuple[NpcEnemy, NpcWeapon | NpcSystem]],
                author_id: int,
            ):
                super().__init__(timeout=30)
                self.chosen: tuple[NpcEnemy, NpcWeapon | NpcSystem] | None = None
                self.author_id = author_id
                for i, (e, f) in enumerate(pairs[:5]):
                    is_w    = isinstance(f, NpcWeapon)
                    ftype   = "Weapon" if is_w else f.system_type
                    icon    = _TYPE_ICON.get(ftype, "▶️")
                    style   = _TYPE_STYLE.get(ftype, discord.ButtonStyle.secondary)
                    label   = f"{icon} {e.display_name} — {f.name}"[:80]
                    btn     = discord.ui.Button(label=label, style=style, row=i)
                    btn.callback = self._make_cb((e, f))
                    self.add_item(btn)

            def _make_cb(self, pair):
                async def cb(interaction: discord.Interaction):
                    if interaction.user.id != self.author_id:
                        await interaction.response.send_message("Not your pick.", ephemeral=True)
                        return
                    self.chosen = pair
                    await interaction.response.defer()
                    self.stop()
                return cb

        view = _PairView(pairs, ctx.author.id)
        lines = [
            f"{'⚔️' if isinstance(f, NpcWeapon) else '🔧'} **{e.display_name}** — **{f.name}**"
            for e, f in pairs[:5]
        ]
        embed = discord.Embed(
            title="🤔 Multiple matches — which one?",
            description="\n".join(lines),
            color=0xFFC107,
        )
        msg = await ctx.reply(embed=embed, view=view, mention_author=False)
        await view.wait()

        if view.chosen is None:
            await msg.edit(content="⏱️ Timed out.", embed=None, view=None)
            return None

        await msg.delete()
        return view.chosen

    # ── Feature execution ─────────────────────────────────────────────────────

    async def _execute_feature(
        self,
        ctx: commands.Context,
        enemy: NpcEnemy,
        feature: NpcWeapon | NpcSystem,
        acc_bonus: int,
        target: "TargetResult | None" = None,
    ) -> None:
        """Dispatch to weapon, tech, or system handler."""
        if isinstance(feature, NpcWeapon):
            await self._use_weapon(ctx, enemy, feature, acc_bonus, target)
        elif isinstance(feature, NpcSystem):
            if feature.system_type == "Tech":
                await self._use_tech(ctx, enemy, feature, acc_bonus, target)
            else:
                await self._use_system(ctx, enemy, feature)

    async def _use_weapon(
        self,
        ctx: commands.Context,
        enemy: NpcEnemy,
        weapon: NpcWeapon,
        acc_bonus: int,
        target: "TargetResult | None" = None,
    ) -> None:
        """Roll attack + damage for an NPC weapon, then show action buttons."""
        # Re-fetch fresh vitals from DB
        fresh = npc_storage.get_enemy(ctx.guild.id, ctx.channel.id, enemy.slug)
        if fresh:
            enemy = fresh

        attack      = NpcAttackResult(
            attack_bonus  = weapon.attack_bonus,
            accuracy_dice = weapon.accuracy + acc_bonus,
        )
        dmg_results = _roll_npc_damage(weapon, attack.crit)

        # Total damage to target (exclude heat-type entries which go to heat button)
        target_hp_dmg = sum(
            (r1 if (r2 is None or r1.total >= r2.total) else r2).total
            for _, r1, r2 in dmg_results
            if _.get("type", "").lower() != "heat"
        )
        heat_self = weapon.heat_self() or 0

        embed = _build_weapon_embed(enemy, weapon, attack, dmg_results, target=target)
        embed.set_author(
            name=f"GM — {ctx.author.display_name}",
            icon_url=ctx.author.display_avatar.url,
        )

        view = NpcActionView(
            guild_id    = ctx.guild.id,
            channel_id  = ctx.channel.id,
            enemy       = enemy,
            self_heat   = heat_self,
            hp_damage   = target_hp_dmg,
            embed       = embed,
            target      = target,
        )
        await ctx.reply(embed=embed, view=view, mention_author=False)

    async def _use_tech(
        self,
        ctx: commands.Context,
        enemy: NpcEnemy,
        system: NpcSystem,
        acc_bonus: int,
        target: "TargetResult | None" = None,
    ) -> None:
        """Roll tech attack for an NPC Tech action."""
        fresh = npc_storage.get_enemy(ctx.guild.id, ctx.channel.id, enemy.slug)
        if fresh:
            enemy = fresh

        attack = NpcAttackResult(
            attack_bonus  = system.attack_bonus or enemy.stats.tech_attack,
            accuracy_dice = acc_bonus,
        )
        annotated, _ = roll_all_dice_in_text(system.effect) if system.effect else ("", [])

        embed = _build_tech_embed(enemy, system, attack, annotated, target=target)
        embed.set_author(
            name=f"GM — {ctx.author.display_name}",
            icon_url=ctx.author.display_avatar.url,
        )

        # Tech attacks deal no direct HP damage by default, but show the
        # target HP button so the GM can apply any manual damage ruling.
        view = NpcActionView(
            guild_id   = ctx.guild.id,
            channel_id = ctx.channel.id,
            enemy      = enemy,
            self_heat  = 0,
            hp_damage  = 0,      # GM decides damage from tech effect text
            embed      = embed,
            target     = target,
        )
        await ctx.reply(embed=embed, view=view, mention_author=False)

    async def _use_system(
        self,
        ctx: commands.Context,
        enemy: NpcEnemy,
        system: NpcSystem,
    ) -> None:
        """Display a System / Trait / Reaction effect, rolling any inline dice."""
        annotated, _ = roll_all_dice_in_text(system.effect) if system.effect else ("", [])

        embed = _build_system_embed(enemy, system, annotated)
        embed.set_author(
            name=f"GM — {ctx.author.display_name}",
            icon_url=ctx.author.display_avatar.url,
        )
        await ctx.reply(embed=embed, mention_author=False)

    # ── Error handlers ────────────────────────────────────────────────────────

    @npc.error
    async def npc_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CommandNotFound):
            await ctx.reply(
                "❓ Unknown subcommand. Run `!npc` for a list of commands.",
                mention_author=False,
            )
        else:
            raise error

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        """Catch-all error handler for the cog."""
        cmd_name = ctx.command.name if ctx.command else ""
        if cmd_name in ("npca", "npcaction", "na"):
            await ctx.reply(
                f"❌ {error}\nUsage: `!npca <enemy> [feature]`",
                mention_author=False,
            )
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(EncounterCog(bot))
