"""
Discord embed builders for NPC / enemy statblocks and encounter tracking.
"""
from __future__ import annotations
import discord
from utils.npc_parser import NpcEnemy, NpcWeapon, NpcSystem
from utils.tags import format_tag

# ── Colour palette ────────────────────────────────────────────────────────────
ROLE_COLORS = {
    "controller": 0x9C27B0,
    "striker": 0xCF2020,
    "artillery": 0xE8871A,
    "defender": 0x2196F3,
    "support": 0x4CAF50,
    "biological": 0x795548,
    "ultra": 0xFF5722,
}
DEFAULT_NPC_COLOR = 0x455A64 # blue-grey

DAMAGE_COLORS = {
    "kinetic": 0x9E9E9E,
    "energy": 0x2196F3,
    "explosive": 0xE8871A,
    "burn": 0xFF5722,
    "heat": 0xFF9800,
}


def _role_color(enemy: NpcEnemy) -> int:
    return ROLE_COLORS.get(enemy.role.lower(), DEFAULT_NPC_COLOR)


def _progress_bar(current: int, maximum: int, length: int = 10) -> str:
    if maximum <= 0:
        return "█"* length
    filled = round(length * max(0, current) / maximum)
    filled = max(0, min(filled, length))
    return "█"* filled + "░"* (length - filled)


# ── Single enemy statblock ─────────────────────────────────────────────────────

def build_npc_statblock_embed(enemy: NpcEnemy) -> discord.Embed:
    """Full statblock embed for one enemy."""
    s = enemy.stats
    tier_stars = ""* enemy.tier + ""* (3 - enemy.tier)

    title = f"{' ' if enemy.is_dead else ''}{'[*] ' if enemy.is_active else ''}{enemy.display_name}"
    desc_parts = [
        f"**{enemy.tag}** · Tier {enemy.tier} {tier_stars} · **{enemy.role.title()}**",
    ]
    if enemy.tactics:
        desc_parts.append(f"*{enemy.tactics[:180]}{'…' if len(enemy.tactics) > 180 else ''}*")

    embed = discord.Embed(
        title=title,
        description="\n".join(desc_parts),
        color=_role_color(enemy),
    )

    # ── Vitals ────────────────────────────────────────────────────────────────
    hp_bar = _progress_bar(s.current_hp, s.hp)
    heat_bar = _progress_bar(s.current_heat, s.heatcap)
    str_bar = _progress_bar(s.current_structure, s.structure)
    sts_bar = _progress_bar(s.current_stress, s.stress)

    embed.add_field(
        name="Vitals",
        value=(
            f"**HP** `{hp_bar}` {s.current_hp}/{s.hp}\n"
            f"**Heat** `{heat_bar}` {s.current_heat}/{s.heatcap}\n"
            f"**Structure** `{str_bar}` {s.current_structure}/{s.structure}\n"
            f"**Stress** `{sts_bar}` {s.current_stress}/{s.stress}"
            + (f"\n**Burn** {s.burn}" if s.burn else "")
            + (f"**Overshield** {s.overshield}" if s.overshield else "")
        ),
        inline=False,
    )

    # ── Combat stats ──────────────────────────────────────────────────────────
    embed.add_field(
        name="Stats",
        value=(
            f"**Armor** {s.armor} · **Size** {s.size} · **Speed** {s.speed}\n"
            f"**EVA** {s.evasion} · **EDEF** {s.edef} · **Save** {s.save_target}\n"
            f"**Sensors** {s.sensor_range} · **Activations** {s.activations}\n"
            f"**Atk Bonus** +{s.attack_bonus} · **Tech Atk** +{s.tech_attack}"
        ),
        inline=False,
    )

    # ── Weapons ───────────────────────────────────────────────────────────────
    if enemy.weapons:
        for w in enemy.weapons:
            parts = [
                f"**{w.weapon_type}** · Atk: **+{w.attack_bonus}**"
                + (f"Acc: +{w.accuracy}" if w.accuracy else ""),
                f"Dmg: **{w.damage_str}** · Range: {w.range_str}",
            ]
            if w.on_hit:
                parts.append(f"*On Hit:* {w.on_hit[:150]}")
            if w.effect:
                parts.append(f"*Effect:* {w.effect[:150]}")
            if w.tags:
                parts.append("Tags: "+ ", ".join(format_tag(t) for t in w.tags))
            embed.add_field(
                name=f"{w.name}",
                value="\n".join(parts),
                inline=False,
            )

    # ── Systems / Traits / Techs ──────────────────────────────────────────────
    traits = [s for s in enemy.systems if s.system_type == "Trait"]
    systems = [s for s in enemy.systems if s.system_type == "System"]
    techs = [s for s in enemy.systems if s.system_type == "Tech"]
    reactions = [s for s in enemy.systems if s.system_type == "Reaction"]

    for group, icon, label in (
        (traits, "", "Traits"),
        (systems, "", "Systems"),
        (techs, "", "Tech Actions"),
        (reactions, "↩️", "Reactions"),
    ):
        if not group:
            continue
        lines = []
        for feat in group:
            line = f"**{feat.name}**"
            if feat.system_type == "Tech":
                line += f"*(Quick Tech, Atk: +{feat.attack_bonus})*"
            if feat.tags:
                tag_str = ", ".join(format_tag(t) for t in feat.tags)
                line += f"[{tag_str}]"
            effect = feat.effect[:200] + ("…" if len(feat.effect) > 200 else "")
            line += f"\n{effect}"
            lines.append(line)
        embed.add_field(
            name=f"{icon} {label}",
            value="\n\n".join(lines)[:1024],
            inline=False,
        )

    embed.set_footer(
        text=f"T{enemy.tier} {enemy.base_name} · Use !npc hp / !npc heat to track vitals"
    )
    return embed


# ── Encounter roster ───────────────────────────────────────────────────────────

def build_encounter_embed(enemies: list[NpcEnemy], title: str = "Encounter Roster") -> discord.Embed:
    """Compact overview of all enemies in the encounter."""
    alive = [e for e in enemies if not e.is_dead]
    dead = [e for e in enemies if e.is_dead]

    embed = discord.Embed(
        title=title,
        description=(
            f"**{len(alive)}** combatant{'s' if len(alive) != 1 else ''} active"
            + (f" · {len(dead)} destroyed" if dead else "")
        ),
        color=0xCF2020,
    )

    if alive:
        lines = []
        for e in alive:
            s = e.stats
            hp_bar = _progress_bar(s.current_hp, s.hp, length=8)
            active_marker = "**[*]**" if e.is_active else " "
            heat_str = f"  Heat {s.current_heat}/{s.heatcap}" if s.current_heat > 0 else ""
            burn_str = f"  Burn {s.burn}" if s.burn else ""
            lines.append(
                f"{active_marker} **{e.display_name}** [{e.role.title()} T{e.tier}]\n"
                f"  HP `{hp_bar}` {s.current_hp}/{s.hp}"
                f"  EVA {s.evasion}  EDEF {s.edef}"
                f"{heat_str}{burn_str}"
            )
        embed.add_field(name="Enemies", value="\n".join(lines)[:1024], inline=False)

    if dead:
        embed.add_field(
            name="Destroyed",
            value=" ".join(f"~~{e.display_name}~~" for e in dead),
            inline=False,
        )

    embed.set_footer(text="!npc list · !npc show <name> · !npc activate <name>")
    return embed


# ── Add-result summary ─────────────────────────────────────────────────────────

def build_add_result_embed(added: list[NpcEnemy]) -> discord.Embed:
    """Shown after !npc add to confirm what was imported."""
    embed = discord.Embed(
        title=f"Added {len(added)} enem{'y' if len(added) == 1 else 'ies'} to encounter",
        color=0x4CAF50,
    )
    lines = []
    for e in added:
        s = e.stats
        lines.append(
            f"**{e.display_name}** — {e.tag} {e.role.title()} T{e.tier} "
            f"{s.hp} HP {s.armor} Armor [*]{s.speed} Spd"
        )
    embed.description = "\n".join(lines)
    embed.set_footer(text="!npc list · !npc show <name> · !npc activate <name>")
    return embed
