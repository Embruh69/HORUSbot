"""
Lancer comp/con JSON parser.
Turns a raw "Save Pilot" export into clean Python dataclasses.
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from typing import Optional


# ─── Pilot ────────────────────────────────────────────────────────────────────

@dataclass
class PilotSkill:
    id: str
    name: str
    description: str
    family: str   # str / dex / int / cha
    rank: int     # 1-6 (+2 per rank, so +2/+4/+6/+8/+10/+12)

    @property
    def bonus(self) -> int:
        return self.rank * 2


@dataclass
class PilotTalent:
    id: str
    name: str
    terse: str
    rank: int     # 1-3

    @property
    def active_ranks(self) -> list[str]:
        """Return the names of all unlocked talent ranks."""
        return [f"Rank {i+1}" for i in range(self.rank)]


@dataclass
class PilotStats:
    hp: int
    armor: int
    speed: int
    evasion: int
    edef: int
    grit: int
    # mech skill HASE
    hull: int
    agility: int
    systems: int
    engineering: int


@dataclass
class Pilot:
    callsign: str
    name: str
    player_name: str
    level: int
    status: str
    background: str
    notes: str
    stats: PilotStats
    skills: list[PilotSkill]
    talents: list[PilotTalent]
    licenses: list[tuple[str, int]]   # (license_id, rank)
    core_bonuses: list[str]           # ids
    favorite_mech_id: Optional[str]


# ─── Mech ─────────────────────────────────────────────────────────────────────

@dataclass
class Weapon:
    id: str
    name: str
    weapon_type: str   # Melee / Rifle / etc.
    mount_size: str    # Aux / Main / Heavy / Superheavy
    damage: list[dict]   # [{type, val}, ...]
    range_data: list[dict]
    tags: list[str]
    sp: int
    effect: str

    @property
    def damage_str(self) -> str:
        parts = []
        for d in self.damage:
            parts.append(f"{d.get('val', '?')} {d.get('type', '')}")
        return " + ".join(parts) if parts else "—"

    @property
    def range_str(self) -> str:
        if not self.range_data:
            return "Melee"
        parts = []
        for r in self.range_data:
            parts.append(f"{r.get('type', '')} {r.get('val', '')}")
        return ", ".join(parts)


@dataclass
class System:
    id: str
    name: str
    sp: int
    tags: list[str]
    effect: str
    type: str


@dataclass
class MechStats:
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
    repair_capacity: int
    sp: int
    size: int | float
    # current hp/heat (for active tracking)
    current_hp: int
    current_heat: int
    current_structure: int
    current_stress: int


@dataclass
class Mech:
    id: str
    name: str
    frame_id: str
    frame_name: str
    frame_source: str
    frame_mechtype: list[str]
    stats: MechStats
    weapons: list[Weapon]
    systems: list[System]
    is_active: bool


# ─── Full Character ────────────────────────────────────────────────────────────

@dataclass
class LancerCharacter:
    pilot: Pilot
    mechs: list[Mech]

    @property
    def active_mech(self) -> Optional[Mech]:
        for m in self.mechs:
            if m.is_active:
                return m
        return self.mechs[0] if self.mechs else None


# ─── Parse helpers ────────────────────────────────────────────────────────────

def _parse_weapon(slot: dict) -> Optional[Weapon]:
    w = slot.get("weapon")
    if not w:
        return None
    data = w.get("data", {})
    return Weapon(
        id=w.get("id", ""),
        name=data.get("name", "Unknown Weapon"),
        weapon_type=data.get("type", "?"),
        mount_size=data.get("mount", slot.get("size", "Main")),
        damage=data.get("damage", []),
        range_data=data.get("range", []),
        tags=[t.get("id", "") for t in data.get("tags", [])],
        sp=data.get("sp", 0),
        effect=data.get("effect", ""),
    )


def _parse_system(s: dict) -> System:
    data = s.get("data", {})
    return System(
        id=s.get("id", ""),
        name=data.get("name", "Unknown System"),
        sp=data.get("sp", 0),
        tags=[t.get("id", "") for t in data.get("tags", [])],
        effect=data.get("effect", ""),
        type=data.get("type", ""),
    )


def _parse_mech_stats(raw: dict, is_active: bool = False) -> MechStats:
    mx = raw.get("max", {})
    cu = raw.get("current", {})

    def c(key, fallback_to_max=True):
        val = cu.get(key)
        if val is None and fallback_to_max:
            val = mx.get(key, 0)
        return val or 0

    def m(key):
        return mx.get(key, 0)

    return MechStats(
        hp=m("hp"),
        armor=m("armor"),
        structure=m("structure"),
        stress=m("stress"),
        heatcap=m("heatcap"),
        speed=m("speed"),
        evasion=m("evasion"),
        edef=m("edef"),
        sensor_range=m("sensorRange"),
        save_target=m("saveTarget"),
        repair_capacity=m("repairCapacity"),
        sp=m("sp"),
        size=m("size"),
        current_hp=c("hp"),
        current_heat=c("heat"),
        current_structure=c("structure"),
        current_stress=c("stress"),
    )


def _parse_mech(raw: dict, favorite_id: Optional[str]) -> Mech:
    fd = raw.get("frameData", {})
    loadout = (raw.get("loadouts") or [{}])[raw.get("active_loadout_index", 0)]

    weapons: list[Weapon] = []
    for mount in loadout.get("mounts", []):
        for slot in mount.get("slots", []):
            w = _parse_weapon(slot)
            if w:
                weapons.append(w)
    # integrated weapons
    for im in loadout.get("integratedMounts", []):
        for slot in im.get("slots", []):
            w = _parse_weapon(slot)
            if w:
                weapons.append(w)

    systems = [_parse_system(s) for s in loadout.get("systems", [])]
    systems += [_parse_system(s) for s in loadout.get("integratedSystems", [])]

    return Mech(
        id=raw.get("id", ""),
        name=raw.get("name", "Unnamed Mech"),
        frame_id=raw.get("frame", ""),
        frame_name=fd.get("name", raw.get("frame", "Unknown Frame")),
        frame_source=fd.get("source", "?"),
        frame_mechtype=fd.get("mechtype", []),
        stats=_parse_mech_stats(raw.get("stats", {})),
        weapons=weapons,
        systems=systems,
        is_active=(raw.get("id") == favorite_id),
    )


def _parse_pilot_stats(raw: dict, mech_skills: list) -> PilotStats:
    mx = raw.get("max", {})
    hull, agility, systems, eng = (mech_skills + [0, 0, 0, 0])[:4]
    return PilotStats(
        hp=mx.get("hp", 0),
        armor=mx.get("armor", 0),
        speed=mx.get("speed", 0),
        evasion=mx.get("evasion", 0),
        edef=mx.get("edef", 0),
        grit=mx.get("grit", 0),
        hull=hull,
        agility=agility,
        systems=systems,
        engineering=eng,
    )


# ─── Main entry point ─────────────────────────────────────────────────────────

def parse_compcon_json(raw: str | bytes | dict) -> LancerCharacter:
    """
    Parse a comp/con "Save Pilot" JSON export.
    Accepts a JSON string, bytes, or an already-decoded dict.
    Returns a LancerCharacter dataclass.
    Raises ValueError with a helpful message on bad input.
    """
    if isinstance(raw, (str, bytes)):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {e}") from e
    else:
        data = raw

    export_type = data.get("EXPORT_TYPE", "")
    if export_type != "Save Pilot":
        raise ValueError(
            f"Expected EXPORT_TYPE 'Save Pilot', got '{export_type}'.\n"
            "Export your pilot from comp/con using **Export → Save Pilot**."
        )

    pilot_data = data.get("data", {})

    # Skills
    skills = []
    for s in pilot_data.get("skills", []):
        sd = s.get("data", {})
        skills.append(PilotSkill(
            id=s.get("id", ""),
            name=sd.get("name", "?"),
            description=sd.get("description", ""),
            family=sd.get("family", ""),
            rank=s.get("rank", 1),
        ))

    # Talents
    talents = []
    for t in pilot_data.get("talents", []):
        td = t.get("data", {})
        talents.append(PilotTalent(
            id=t.get("id", ""),
            name=td.get("name", "?"),
            terse=td.get("terse", ""),
            rank=t.get("rank", 1),
        ))

    # Licenses  (id might be "mf_manticore" → strip "mf_" for display)
    licenses = [
        (lic.get("id", "?"), lic.get("rank", 0))
        for lic in pilot_data.get("licenses", [])
    ]

    # Core bonuses
    core_bonuses = [cb.get("id", "") for cb in pilot_data.get("core_bonuses", [])]

    mech_skills = pilot_data.get("mechSkills", [0, 0, 0, 0])
    pilot_stats = _parse_pilot_stats(pilot_data.get("stats", {}), mech_skills)

    pilot = Pilot(
        callsign=pilot_data.get("callsign", "Unknown"),
        name=pilot_data.get("name", ""),
        player_name=pilot_data.get("player_name", ""),
        level=pilot_data.get("level", 0),
        status=pilot_data.get("status", "Active"),
        background=pilot_data.get("background", ""),
        notes=pilot_data.get("notes", ""),
        stats=pilot_stats,
        skills=skills,
        talents=talents,
        licenses=licenses,
        core_bonuses=core_bonuses,
        favorite_mech_id=pilot_data.get("favorite_mech"),
    )

    favorite_id = pilot_data.get("favorite_mech")
    mechs = [_parse_mech(m, favorite_id) for m in pilot_data.get("mechs", [])]

    return LancerCharacter(pilot=pilot, mechs=mechs)
