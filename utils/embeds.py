"""
Discord embed builders for Lancer character sheets.
Produces Avrae-style embeds showing pilot and mech info.
"""
from __future__ import annotations
import discord
from utils.parser import LancerCharacter, Pilot, Mech

# ─── Colour palette (Lancer-ish) ──────────────────────────────────────────────
LANCER_RED    = 0xCF2020
LANCER_ORANGE = 0xE8871A
LANCER_BLUE   = 0x2196F3
LANCER_TEAL   = 0x00BCD4
LANCER_DARK   = 0x1A1A2E

# One colour per manufacturer (best-effort lookup)
SOURCE_COLORS = {
    "GMS":      0x4CAF50,
    "IPS-N":    0x2196F3,
    "SSC":      0xE91E63,
    "HORUS":    0x9C27B0,
    "HA":       0xE8871A,
}


def _mech_colour(mech: Mech) -> int:
    return SOURCE_COLORS.get(mech.frame_source.upper(), LANCER_RED)


def _progress_bar(current: int, maximum: int, length: int = 10) -> str:
    """Simple Unicode progress bar."""
    if maximum <= 0:
        return "█" * length
    filled = round(length * current / maximum)
    filled = max(0, min(filled, length))
    return "█" * filled + "░" * (length - filled)


def _lic_name(lic_id: str) -> str:
    """Turn 'mf_manticore' → 'Manticore'."""
    return lic_id.removeprefix("mf_").replace("_", " ").title()


# ─── Pilot sheet ──────────────────────────────────────────────────────────────

def build_pilot_embed(char: LancerCharacter) -> discord.Embed:
    p = char.pilot
    title = f"🪖 {p.callsign}"
    if p.name:
        title += f"  ·  {p.name}"
    desc_parts = [f"**LL{p.level}** Pilot  ·  Status: **{p.status}**"]
    if p.player_name:
        desc_parts.append(f"Player: {p.player_name}")

    embed = discord.Embed(
        title=title,
        description="\n".join(desc_parts),
        color=LANCER_RED,
    )

    # ── Pilot stats ──────────────────────────────────────────────────────────
    s = p.stats
    embed.add_field(
        name="📊 Pilot Stats",
        value=(
            f"**HP** {s.hp}  ·  **Armor** {s.armor}  ·  **Grit** +{s.grit}\n"
            f"**Speed** {s.speed}  ·  **Evasion** {s.evasion}  ·  **E-Defense** {s.edef}"
        ),
        inline=False,
    )

    # ── HASE ─────────────────────────────────────────────────────────────────
    embed.add_field(
        name="⚙️ Mech Skills (HASE)",
        value=(
            f"**H**ull `{s.hull:+d}`  ·  **A**gi `{s.agility:+d}`  ·  "
            f"**S**ys `{s.systems:+d}`  ·  **E**ng `{s.engineering:+d}`"
        ),
        inline=False,
    )

    # ── Skills ───────────────────────────────────────────────────────────────
    if p.skills:
        skill_lines = [
            f"`+{sk.bonus:2d}` **{sk.name}**" for sk in
            sorted(p.skills, key=lambda x: -x.bonus)
        ]
        embed.add_field(
            name=f"🎯 Skills ({len(p.skills)})",
            value="\n".join(skill_lines) or "—",
            inline=True,
        )

    # ── Talents ───────────────────────────────────────────────────────────────
    if p.talents:
        talent_lines = [
            f"{'★' * t.rank}{'☆' * (3 - t.rank)} **{t.name}**"
            for t in p.talents
        ]
        embed.add_field(
            name=f"✨ Talents ({len(p.talents)})",
            value="\n".join(talent_lines) or "—",
            inline=True,
        )

    # ── Licenses ─────────────────────────────────────────────────────────────
    if p.licenses:
        lic_lines = [
            f"{'◆' * rank} {_lic_name(lic_id)}"
            for lic_id, rank in p.licenses
        ]
        embed.add_field(
            name="📜 Licenses",
            value="\n".join(lic_lines) or "—",
            inline=False,
        )

    # ── Mechs owned ──────────────────────────────────────────────────────────
    if char.mechs:
        mech_lines = []
        for m in char.mechs:
            active_marker = "★ " if m.is_active else "  "
            mech_lines.append(
                f"{active_marker}**{m.name}**  ·  "
                f"{m.frame_source} {m.frame_name}"
            )
        embed.add_field(
            name="🤖 Mechs",
            value="\n".join(mech_lines),
            inline=False,
        )

    embed.set_footer(text="Use !sheet mech to see your active mech · !help for more commands")
    return embed


# ─── Mech sheet ───────────────────────────────────────────────────────────────

def build_mech_embed(char: LancerCharacter, mech: Mech) -> discord.Embed:
    s = mech.stats
    mechtype_str = " / ".join(mech.frame_mechtype) if mech.frame_mechtype else ""

    embed = discord.Embed(
        title=f"🤖 {mech.name}",
        description=(
            f"**{mech.frame_source} {mech.frame_name}**"
            + (f"  ·  {mechtype_str}" if mechtype_str else "")
            + f"\nSize **{s.size}**  ·  SP **{s.sp}**"
        ),
        color=_mech_colour(mech),
    )

    # ── Defensive stats ──────────────────────────────────────────────────────
    hp_bar = _progress_bar(s.current_hp, s.hp)
    heat_bar = _progress_bar(s.current_heat, s.heatcap)
    struct_bar = _progress_bar(s.current_structure, s.structure)
    stress_bar = _progress_bar(s.current_stress, s.stress)

    embed.add_field(
        name="❤️ Vitals",
        value=(
            f"**HP**       `{hp_bar}` {s.current_hp}/{s.hp}\n"
            f"**Heat**     `{heat_bar}` {s.current_heat}/{s.heatcap}\n"
            f"**Structure** `{struct_bar}` {s.current_structure}/{s.structure}\n"
            f"**Stress**   `{stress_bar}` {s.current_stress}/{s.stress}"
        ),
        inline=False,
    )

    embed.add_field(
        name="🛡️ Defense",
        value=(
            f"**Armor** {s.armor}  ·  **Evasion** {s.evasion}  ·  "
            f"**E-Defense** {s.edef}\n"
            f"**Speed** {s.speed}  ·  **Sensors** {s.sensor_range}  ·  "
            f"**Save Target** {s.save_target}\n"
            f"**Repair Cap** {s.repair_capacity}"
        ),
        inline=False,
    )

    # ── Weapons ──────────────────────────────────────────────────────────────
    if mech.weapons:
        weapon_lines = []
        for w in mech.weapons:
            line = f"**{w.name}** ({w.weapon_type})"
            dmg = w.damage_str
            if dmg and dmg != "—":
                line += f"  —  {dmg}"
            rng = w.range_str
            if rng and rng != "Melee":
                line += f"  ·  Range: {rng}"
            weapon_lines.append(line)
        embed.add_field(
            name=f"⚔️ Weapons ({len(mech.weapons)})",
            value="\n".join(weapon_lines),
            inline=False,
        )

    # ── Systems ───────────────────────────────────────────────────────────────
    if mech.systems:
        sys_lines = [f"**{sys.name}**" + (f" (SP {sys.sp})" if sys.sp else "") for sys in mech.systems]
        embed.add_field(
            name=f"🔧 Systems ({len(mech.systems)})",
            value="\n".join(sys_lines),
            inline=False,
        )

    embed.set_footer(
        text=f"Pilot: {char.pilot.callsign} (LL{char.pilot.level}) · "
             "Use !sheet pilot to view pilot stats"
    )
    return embed


# ─── Quick summary (both on one embed) ────────────────────────────────────────

def build_summary_embed(char: LancerCharacter) -> discord.Embed:
    """Compact overview: pilot basics + active mech basics side by side."""
    p = char.pilot
    m = char.active_mech

    embed = discord.Embed(
        title=f"📋 {p.callsign}" + (f"  ·  {p.name}" if p.name else ""),
        description=f"**LL{p.level}** Pilot",
        color=_mech_colour(m) if m else LANCER_RED,
    )

    # Pilot column
    ps = p.stats
    embed.add_field(
        name="🪖 Pilot",
        value=(
            f"HP **{ps.hp}**  ·  Armor **{ps.armor}**\n"
            f"EVA **{ps.evasion}**  ·  EDEF **{ps.edef}**\n"
            f"Grit **+{ps.grit}**  ·  Speed **{ps.speed}**\n"
            f"**HASE** H{ps.hull}/A{ps.agility}/S{ps.systems}/E{ps.engineering}"
        ),
        inline=True,
    )

    # Active mech column
    if m:
        ms = m.stats
        embed.add_field(
            name=f"🤖 {m.name} ({m.frame_source} {m.frame_name})",
            value=(
                f"HP **{ms.hp}**  ·  Heat **{ms.heatcap}**\n"
                f"Struct **{ms.structure}**  ·  Stress **{ms.stress}**\n"
                f"EVA **{ms.evasion}**  ·  EDEF **{ms.edef}**\n"
                f"Speed **{ms.speed}**  ·  Sensors **{ms.sensor_range}**"
            ),
            inline=True,
        )

    top_skills = sorted(p.skills, key=lambda x: -x.bonus)[:5]
    if top_skills:
        embed.add_field(
            name="Top Skills",
            value="  ".join(f"`+{sk.bonus} {sk.name}`" for sk in top_skills),
            inline=False,
        )

    embed.set_footer(text="!sheet pilot  ·  !sheet mech  ·  !sheet weapons  ·  !sheet systems")
    return embed


# ─── Weapons detail embed ─────────────────────────────────────────────────────

def build_weapons_embed(char: LancerCharacter, mech: Mech) -> discord.Embed:
    embed = discord.Embed(
        title=f"⚔️ {mech.name} — Weapons",
        color=_mech_colour(mech),
    )
    if not mech.weapons:
        embed.description = "No weapons equipped."
        return embed

    for w in mech.weapons:
        value_parts = [
            f"**Type:** {w.weapon_type}",
            f"**Damage:** {w.damage_str}",
            f"**Range:** {w.range_str}",
        ]
        if w.effect:
            # Truncate long effect text for embed limits
            effect = w.effect[:300] + ("…" if len(w.effect) > 300 else "")
            value_parts.append(f"**Effect:** {effect}")
        if w.tag_ids:
            value_parts.append(f"**Tags:** {', '.join(w.tag_ids)}")

        embed.add_field(name=w.name, value="\n".join(value_parts), inline=False)

    return embed


# ─── Systems detail embed ─────────────────────────────────────────────────────

def build_systems_embed(char: LancerCharacter, mech: Mech) -> discord.Embed:
    embed = discord.Embed(
        title=f"🔧 {mech.name} — Systems",
        color=_mech_colour(mech),
    )
    if not mech.systems:
        embed.description = "No systems equipped."
        return embed

    for sys in mech.systems:
        value_parts = []
        if sys.sp:
            value_parts.append(f"**SP:** {sys.sp}")
        if sys.type:
            value_parts.append(f"**Type:** {sys.type}")
        if sys.effect:
            effect = sys.effect[:300] + ("…" if len(sys.effect) > 300 else "")
            value_parts.append(f"**Effect:** {effect}")
        if sys.tag_ids:
            value_parts.append(f"**Tags:** {', '.join(sys.tag_ids)}")

        embed.add_field(
            name=sys.name,
            value="\n".join(value_parts) if value_parts else "No details.",
            inline=False,
        )

    return embed


# ─── Talent detail embed ──────────────────────────────────────────────────────

def build_talents_embed(char: LancerCharacter) -> discord.Embed:
    p = char.pilot
    embed = discord.Embed(
        title=f"✨ {p.callsign} — Talents",
        color=LANCER_RED,
    )
    for t in p.talents:
        stars = "★" * t.rank + "☆" * (3 - t.rank)
        embed.add_field(
            name=f"{stars} {t.name}  (Rank {t.rank})",
            value=t.terse or "—",
            inline=False,
        )
    return embed