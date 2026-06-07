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
) -> discord.Embed:
    embed = discord.Embed(
        title=f"⚔️ {enemy.display_name} — {weapon.name}",
        color=_weapon_color(weapon),
    )
    embed.description = (
        f"**{weapon.weapon_type}**  ·  Range: {weapon.range_str}  "
        f"·  *{enemy.tag} {enemy.role.title()} T{enemy.tier}*"
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

    embed.set_footer(text=f"{enemy.display_name}  ·  Use the button below to apply self-heat")
    return embed


def _build_tech_embed(
    enemy: NpcEnemy,
    system: NpcSystem,
    attack: NpcAttackResult,
    annotated_text: str,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"💻 {enemy.display_name} — {system.name}",
        color=0x7E57C2,
    )
    embed.description = (
        f"**{system.tech_type or 'Quick'} Tech**  ·  "
        f"*{enemy.tag} {enemy.role.title()} T{enemy.tier}*"
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

    embed.set_footer(text=f"{enemy.display_name}")
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


class NpcHeatView(discord.ui.View):
    """
    One button: apply self-heat to this NPC in the DB.
    Updates the original embed with the new heat bar on press.
    """
    def __init__(
        self,
        guild_id: int,
        channel_id: int,
        enemy: NpcEnemy,
        self_heat: int,
        original_embed: discord.Embed,
    ):
        super().__init__(timeout=120)
        self.guild_id       = guild_id
        self.channel_id     = channel_id
        self.enemy          = enemy
        self.self_heat      = self_heat
        self.original_embed = original_embed

        if self_heat <= 0:
            self.apply_heat_btn.disabled = True
            self.apply_heat_btn.label    = "No self-heat"
        else:
            self.apply_heat_btn.label = f"🌡️ Apply +{self_heat} Heat to {enemy.display_name}"

    @discord.ui.button(style=discord.ButtonStyle.primary, row=0)
    async def apply_heat_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        enemy = npc_storage.get_enemy(self.guild_id, self.channel_id, self.enemy.slug)
        if enemy is None:
            await interaction.response.send_message("❌ Enemy not found.", ephemeral=True)
            return

        s = enemy.stats
        old_heat    = s.current_heat
        new_heat_raw = old_heat + self.self_heat
        overloaded  = new_heat_raw > s.heatcap and s.heatcap > 0
        new_heat    = max(0, min(s.heatcap, new_heat_raw))

        npc_storage.update_enemy_vitals(
            self.guild_id, self.channel_id, enemy.slug, heat=new_heat
        )

        button.disabled = True
        button.label    = f"✅ +{self.self_heat} Heat Applied"
        button.style    = discord.ButtonStyle.success

        # Update heat field in the original embed
        bar = _progress_bar(new_heat, s.heatcap)
        dz  = "  🌡️ **DANGER ZONE!**" if new_heat >= s.heatcap // 2 else ""
        overheat_str = "\n☢️ **OVERHEATED** — NPC takes 2 AP Energy, Impaired/Slowed." if overloaded else ""

        # Patch the "Self Heat" field if it exists, otherwise add it
        patched = False
        for f in self.original_embed.fields:
            if f.name == "🌡️ Self Heat":
                # discord.py Embed.set_field_at needs index — find it
                idx = list(self.original_embed.fields).index(f)
                self.original_embed.set_field_at(
                    idx,
                    name="🌡️ Self Heat",
                    value=(
                        f"**+{self.self_heat}** applied  ·  "
                        f"`{bar}` **{new_heat}/{s.heatcap}**{dz}{overheat_str}"
                    ),
                    inline=False,
                )
                patched = True
                break

        if not patched:
            self.original_embed.add_field(
                name="🌡️ Self Heat",
                value=(
                    f"**+{self.self_heat}** applied  ·  "
                    f"`{bar}` **{new_heat}/{s.heatcap}**{dz}{overheat_str}"
                ),
                inline=False,
            )

        await interaction.response.edit_message(embed=self.original_embed, view=self)


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
                "`!npca <enemy>`                 — pick an action interactively\n"
                "`!npca <enemy> <feature>`       — use that weapon/system directly\n"
                "`!npca barricade graviton lance` — weapon: attack + damage roll\n"
                "`!npca barricade drag down`      — tech: tech attack roll\n"
                "`!npca barricade mobile printer` — system: shows effect\n"
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
        Use an NPC's weapon or system.

        Usage
        ─────
            !npca <enemy>                  — interactive feature picker
            !npca <enemy> <feature>        — use that feature directly
            !npca barricade graviton lance  — rolls attack + damage
            !npca barricade drag down       — rolls tech attack
            !npca barricade mobile printer  — shows system effect
            !npca barricade a bulwark mods  — trait description
        Add `acc N` or `a N` at the end for accuracy bonus dice.

        Name resolution: partial, case-insensitive.
        Enemies with spaces in names: most-specific match wins.
        """
        if not args:
            await ctx.reply(
                "Usage: `!npca <enemy> [feature]`\nExample: `!npca barricade graviton lance`",
                mention_author=False,
            )
            return

        # ── Parse accuracy flag ───────────────────────────────────────────────
        text      = " ".join(args)
        acc_bonus = 0
        acc_m = re.search(r"\b(?:acc(?:uracy)?)\s*(\d+)", text, re.IGNORECASE)
        if not acc_m:
            acc_m = re.search(r"\ba(\d+)\b", text, re.IGNORECASE)
        if acc_m:
            acc_bonus = int(acc_m.group(1))
            text = text[:acc_m.start()] + text[acc_m.end():]   # strip from query

        text = text.strip()

        # ── Find enemy: try longest prefix match ──────────────────────────────
        enemies = npc_storage.list_enemies(ctx.guild.id, ctx.channel.id)
        if not enemies:
            await ctx.reply(
                "❌ No enemies in this encounter. Use `!npc add` to add some.",
                mention_author=False,
            )
            return

        enemy: NpcEnemy | None = None
        feature_query           = ""

        # Try consuming more and more words as the enemy name, feature = rest
        words = text.split()
        for split in range(len(words), 0, -1):
            candidate_name  = " ".join(words[:split])
            candidate_feat  = " ".join(words[split:])
            e = npc_storage.resolve_enemy_slug(ctx.guild.id, ctx.channel.id, candidate_name)
            if e:
                enemy         = e
                feature_query = candidate_feat
                break

        if enemy is None:
            names = ", ".join(f"**{e.display_name}**" for e in enemies)
            await ctx.reply(
                f"❌ No enemy matching that name. Available: {names}",
                mention_author=False,
            )
            return

        # ── No feature → interactive picker ──────────────────────────────────
        if not feature_query:
            view = FeaturePickerView(enemy, ctx.author.id)
            all_f = list(enemy.weapons) + list(enemy.systems)

            embed = discord.Embed(
                title=f"🎲 {enemy.display_name} — Choose an Action",
                description=(
                    f"**{enemy.tag}** {enemy.role.title()} T{enemy.tier}  ·  "
                    f"❤️ {enemy.stats.current_hp}/{enemy.stats.hp} HP  "
                    f"🌡️ {enemy.stats.current_heat}/{enemy.stats.heatcap} Heat"
                ),
                color=_enemy_color(enemy),
            )

            # Build a readable action list grouped by type
            for group_type, icon, label in [
                ("Weapon",   "⚔️", "Weapons"),
                ("Tech",     "💻", "Tech Actions"),
                ("System",   "🔧", "Systems"),
                ("Trait",    "🧬", "Traits"),
                ("Reaction", "↩️", "Reactions"),
            ]:
                items = (
                    [w for w in enemy.weapons] if group_type == "Weapon"
                    else [s for s in enemy.systems if s.system_type == group_type]
                )
                if not items:
                    continue
                lines = []
                for feat in items:
                    if isinstance(feat, NpcWeapon):
                        lines.append(f"**{feat.name}** — {feat.weapon_type}, {feat.damage_str}, Atk +{feat.attack_bonus}")
                    else:
                        effect_snippet = feat.effect[:80] + ("…" if len(feat.effect) > 80 else "")
                        lines.append(f"**{feat.name}** — {effect_snippet}")
                embed.add_field(name=f"{icon} {label}", value="\n".join(lines), inline=False)

            embed.set_footer(text="Select an action below, or run !npca <enemy> <feature name>")
            msg = await ctx.reply(embed=embed, view=view, mention_author=False)
            await view.wait()

            if view.chosen is None:
                await msg.edit(content="⏱️ Timed out or cancelled.", embed=None, view=None)
                return

            # Edit out the picker, then execute
            await msg.edit(embed=None, view=None, content=f"🎲 **{enemy.display_name}** uses **{view.chosen.name}**…")
            await self._execute_feature(ctx, enemy, view.chosen, acc_bonus)
            return

        # ── Feature query provided → fuzzy search ─────────────────────────────
        matches = _fuzzy_feature(feature_query, enemy)

        if not matches:
            all_names = ", ".join(
                f"**{f.name}**"
                for f in list(enemy.weapons) + list(enemy.systems)
            )
            await ctx.reply(
                f"❌ No feature matching **\"{feature_query}\"** on {enemy.display_name}.\n"
                f"Available: {all_names}",
                mention_author=False,
            )
            return

        if len(matches) == 1:
            async with ctx.typing():
                await self._execute_feature(ctx, enemy, matches[0], acc_bonus)
            return

        # Multiple matches → ask
        view = FeatureDisambiguateView(matches, ctx.author.id)
        embed = discord.Embed(
            title=f"🤔 Multiple matches on {enemy.display_name}",
            description="\n".join(
                f"{'⚔️' if isinstance(m, NpcWeapon) else '🔧'} **{m.name}**"
                for m in matches[:5]
            ),
            color=0xFFC107,
        )
        msg = await ctx.reply(embed=embed, view=view, mention_author=False)
        await view.wait()

        if view.chosen is None:
            await msg.edit(content="⏱️ Timed out.", embed=None, view=None)
            return

        await msg.delete()
        async with ctx.typing():
            await self._execute_feature(ctx, enemy, view.chosen, acc_bonus)

    # ── Feature execution ─────────────────────────────────────────────────────

    async def _execute_feature(
        self,
        ctx: commands.Context,
        enemy: NpcEnemy,
        feature: NpcWeapon | NpcSystem,
        acc_bonus: int,
    ) -> None:
        """Dispatch to weapon, tech, or system handler."""
        if isinstance(feature, NpcWeapon):
            await self._use_weapon(ctx, enemy, feature, acc_bonus)
        elif isinstance(feature, NpcSystem):
            if feature.system_type == "Tech":
                await self._use_tech(ctx, enemy, feature, acc_bonus)
            else:
                await self._use_system(ctx, enemy, feature)

    async def _use_weapon(
        self,
        ctx: commands.Context,
        enemy: NpcEnemy,
        weapon: NpcWeapon,
        acc_bonus: int,
    ) -> None:
        """Roll attack + damage for an NPC weapon, then show heat button."""
        # Re-fetch fresh vitals from DB
        fresh = npc_storage.get_enemy(ctx.guild.id, ctx.channel.id, enemy.slug)
        if fresh:
            enemy = fresh

        attack = NpcAttackResult(
            attack_bonus  = weapon.attack_bonus,
            accuracy_dice = weapon.accuracy + acc_bonus,
        )
        dmg_results = _roll_npc_damage(weapon, attack.crit)

        embed = _build_weapon_embed(enemy, weapon, attack, dmg_results)
        embed.set_author(
            name=f"GM — {ctx.author.display_name}",
            icon_url=ctx.author.display_avatar.url,
        )

        # Self-heat button
        heat_self = weapon.heat_self() or 0
        view = NpcHeatView(ctx.guild.id, ctx.channel.id, enemy, heat_self, embed)

        await ctx.reply(embed=embed, view=view, mention_author=False)

    async def _use_tech(
        self,
        ctx: commands.Context,
        enemy: NpcEnemy,
        system: NpcSystem,
        acc_bonus: int,
    ) -> None:
        """Roll tech attack for an NPC Tech action."""
        fresh = npc_storage.get_enemy(ctx.guild.id, ctx.channel.id, enemy.slug)
        if fresh:
            enemy = fresh

        # Tech actions use tech_attack stat as base, accuracy from feature
        attack = NpcAttackResult(
            attack_bonus  = system.attack_bonus or enemy.stats.tech_attack,
            accuracy_dice = acc_bonus,
        )
        annotated, _ = roll_all_dice_in_text(system.effect) if system.effect else ("", [])

        embed = _build_tech_embed(enemy, system, attack, annotated)
        embed.set_author(
            name=f"GM — {ctx.author.display_name}",
            icon_url=ctx.author.display_avatar.url,
        )
        # No self-heat on tech actions by default
        await ctx.reply(embed=embed, mention_author=False)

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

    @npca.error
    async def npca_error(self, ctx: commands.Context, error: commands.CommandError):
        await ctx.reply(
            f"❌ Error: {error}\nUsage: `!npca <enemy> [feature]`",
            mention_author=False,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(EncounterCog(bot))