"""
NPC / Enemy JSON parser for the Lancer bot.

Parses comp/con NPC unit exports (the format produced by the CORE NPCs LCP
and similar sources) into clean Python dataclasses.

Key differences from pilot JSON
────────────────────────────────
• Stats are tier-indexed lists in class.data.stats, not flat values.
• The active tier is stored at the top level as `tier` (1-indexed).
• Weapons store damage as {"type": "Energy", "damage": [t1,t2,t3]}.
• Features have types: Weapon | System | Trait | Tech | Reaction.
• HP, heat, etc. come from combat_data.stats.max (already computed for the
  selected tier by comp/con).
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from typing import Optional


# ─── NPC Feature dataclasses ──────────────────────────────────────────────────

@dataclass
class NpcWeapon:
    id: str
    name: str
    weapon_type: str          # "Main Cannon", "Heavy Melee", etc.
    tier: int                 # 1-indexed, used to pick tier values
    attack_bonus: int         # already resolved for tier
    accuracy: int             # already resolved for tier
    damage: list[dict]        # [{"type": "Energy", "val": 2}, ...]  — normalised
    range_data: list[dict]    # [{"type": "Range", "val": 10}]
    tags: list[dict]
    effect: str
    on_hit: str

    @property
    def damage_str(self) -> str:
        parts = []
        for d in self.damage:
            parts.append(f"{d['val']} {d['type']}")
        return " + ".join(parts) if parts else "—"

    @property
    def range_str(self) -> str:
        if not self.range_data:
            return "Melee"
        return ", ".join(f"{r['type']} {r['val']}" for r in self.range_data)

    def heat_self(self) -> Optional[int]:
        for t in self.tags:
            if t.get("id") == "tg_heat_self":
                return int(t.get("val", 1))
        return None


@dataclass
class NpcSystem:
    id: str
    name: str
    system_type: str          # "System" | "Tech" | "Trait" | "Reaction"
    tech_type: str            # "Quick" | "Full" | "" — only for Tech features
    attack_bonus: int         # for Tech attacks, 0 otherwise
    effect: str
    tags: list[dict]

    @property
    def tag_ids(self) -> list[str]:
        return [t.get("id", "") for t in self.tags]


@dataclass
class NpcStats:
    hp: int
    armor: int
    structure: int
    stress: int
    heatcap: int
    speed: int
    evasion: int
    edef: int
    sensor_range: int
    save_target: int
    size: int | float
    attack_bonus: int
    tech_attack: int
    activations: int
    # live values (mutated during play)
    current_hp: int
    current_heat: int
    current_structure: int
    current_stress: int
    burn: int
    overshield: int


@dataclass
class NpcEnemy:
    """A single NPC enemy unit ready for combat."""
    # Identity
    base_name: str            # e.g. "BARRICADE"
    display_name: str         # e.g. "BARRICADE A" (set at add-time)
    npc_type: str             # "unit" | "vehicle" | etc.
    tag: str                  # "Mech" | "Biological" | etc.
    role: str                 # "controller" | "striker" | etc.
    tier: int
    flavor: str
    tactics: str
    # Stats & features
    stats: NpcStats
    weapons: list[NpcWeapon]
    systems: list[NpcSystem]  # includes Traits, Techs, Reactions
    # State
    is_active: bool = False   # selected for current turn
    is_dead: bool = False

    @property
    def slug(self) -> str:
        """Lowercase, spaces→underscores — used as storage key component."""
        return self.display_name.lower().replace(" ", "_")


# ─── Parse helpers ────────────────────────────────────────────────────────────

def _clean_html(text: str) -> str:
    import re
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def _tier_val(lst: list, tier: int, default=0):
    """Pick the tier-indexed value from a list; tier is 1-based."""
    idx = max(0, min(tier - 1, len(lst) - 1))
    try:
        return lst[idx]
    except (IndexError, TypeError):
        return default


def _parse_npc_weapon(feature: dict, tier: int) -> NpcWeapon:
    fd = feature["data"]
    t_idx = tier - 1

    # Damage: each entry has {"type": "Energy", "damage": [t1,t2,t3]}
    # Normalise to {"type": "Energy", "val": <tier_value>}
    raw_dmg = fd.get("damage", [])
    damage: list[dict] = []
    for d in raw_dmg:
        dmg_list = d.get("damage", [])
        val = _tier_val(dmg_list, tier) if dmg_list else d.get("val", 0)
        damage.append({"type": d.get("type", "?"), "val": val})

    return NpcWeapon(
        id=fd.get("id", ""),
        name=fd.get("name", "Unknown Weapon"),
        weapon_type=fd.get("weapon_type", "?"),
        tier=tier,
        attack_bonus=_tier_val(fd.get("attack_bonus", [0]), tier),
        accuracy=_tier_val(fd.get("accuracy", [0]), tier),
        damage=damage,
        range_data=fd.get("range", []),
        tags=fd.get("tags", []),
        effect=_clean_html(fd.get("effect", "") or ""),
        on_hit=_clean_html(fd.get("on_hit", "") or ""),
    )


def _parse_npc_system(feature: dict, tier: int) -> NpcSystem:
    fd = feature["data"]
    ftype = fd.get("type", "System")   # System | Trait | Tech | Reaction
    return NpcSystem(
        id=fd.get("id", ""),
        name=fd.get("name", "Unknown"),
        system_type=ftype,
        tech_type=fd.get("tech_type", ""),
        attack_bonus=_tier_val(fd.get("attack_bonus", [0]), tier),
        effect=_clean_html(fd.get("effect", "") or ""),
        tags=fd.get("tags", []),
    )


def _parse_npc_stats(combat_data: dict) -> NpcStats:
    mx = combat_data.get("stats", {}).get("max", {})
    cu = combat_data.get("stats", {}).get("current", {})

    def m(key, default=0):
        return mx.get(key, default)

    def c(key):
        val = cu.get(key)
        if val is None:
            val = mx.get(key, 0)
        return val or 0

    # heatcap in current is sometimes 0 (comp/con bug); fall back to max
    heatcap = m("heatcap") or 10

    return NpcStats(
        hp=m("hp"),
        armor=m("armor"),
        structure=m("structure", 1),
        stress=m("stress", 1),
        heatcap=heatcap,
        speed=m("speed"),
        evasion=m("evasion"),
        edef=m("edef"),
        sensor_range=m("sensorRange"),
        save_target=m("saveTarget"),
        size=m("size", 1),
        attack_bonus=m("attackBonus"),
        tech_attack=m("techAttack"),
        activations=m("activations", 1),
        current_hp=c("hp"),
        current_heat=c("heat"),
        current_structure=c("structure") or m("structure", 1),
        current_stress=c("stress") or m("stress", 1),
        burn=c("burn"),
        overshield=c("overshield"),
    )


# ─── Main entry point ─────────────────────────────────────────────────────────

def parse_npc_json(raw: str | bytes | dict, display_name: str | None = None) -> NpcEnemy:
    """
    Parse a comp/con NPC unit export.

    display_name overrides the name stored in the JSON (used for A/B suffixes).
    Raises ValueError on bad input.
    """
    if isinstance(raw, (str, bytes)):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {e}") from e
    else:
        data = raw

    npc_type = data.get("npcType", "")
    if not npc_type:
        raise ValueError(
            "This doesn't look like a comp/con NPC export "
            "(missing 'npcType' field)."
        )

    tier = data.get("tier", 1)
    base_name = data.get("name", "Unknown NPC")
    dname = display_name if display_name else base_name

    class_data = data.get("class", {}).get("data", {})
    info = class_data.get("info", {})
    role = class_data.get("role", "")
    flavor = _clean_html(info.get("flavor", ""))
    tactics = _clean_html(info.get("tactics", ""))

    combat_data = data.get("combat_data", {})
    stats = _parse_npc_stats(combat_data)

    weapons: list[NpcWeapon] = []
    systems: list[NpcSystem] = []

    for feature in data.get("features", []):
        fd = feature.get("data", {})
        ftype = fd.get("type", "")
        if ftype == "Weapon":
            weapons.append(_parse_npc_weapon(feature, tier))
        else:
            systems.append(_parse_npc_system(feature, tier))

    return NpcEnemy(
        base_name=base_name,
        display_name=dname,
        npc_type=npc_type,
        tag=data.get("tag", ""),
        role=role,
        tier=tier,
        flavor=flavor,
        tactics=tactics,
        stats=stats,
        weapons=weapons,
        systems=systems,
        is_active=False,
        is_dead=False,
    )
