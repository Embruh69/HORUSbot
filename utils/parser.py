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
    rank: int     # 1-6 (+2 per rank)

    @property
    def bonus(self) -> int:
        return self.rank * 2


@dataclass
class PilotTalent:
    id: str
    name: str
    terse: str
    rank: int

    @property
    def active_ranks(self) -> list[str]:
        return [f"Rank {i+1}" for i in range(self.rank)]


@dataclass
class PilotStats:
    hp: int
    armor: int
    speed: int
    evasion: int
    edef: int
    grit: int
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
    licenses: list[tuple[str, int]]
    core_bonuses: list[str]
    favorite_mech_id: Optional[str]


# ─── Mech ─────────────────────────────────────────────────────────────────────

@dataclass
class Weapon:
    id: str
    name: str
    weapon_type: str        # Melee / Rifle / etc.
    mount_size: str         # Aux / Main / Heavy / Superheavy
    damage: list[dict]      # [{type, val, ap?}, ...]
    range_data: list[dict]
    tags: list[dict]        # raw tag objects: [{id, val?}, ...]
    sp: int
    effect: str
    description: str
    on_hit: str
    source: str
    license_name: str

    @property
    def damage_str(self) -> str:
        parts = []
        for d in self.damage:
            ap = " AP" if d.get("ap") else ""
            parts.append(f"{d.get('val', '?')} {d.get('type', '')}{ap}")
        return " + ".join(parts) if parts else "—"

    @property
    def range_str(self) -> str:
        if not self.range_data:
            return "Melee"
        parts = []
        for r in self.range_data:
            parts.append(f"{r.get('type', '')} {r.get('val', '')}")
        return ", ".join(parts)

    @property
    def tag_ids(self) -> list[str]:
        return [t.get("id", "") for t in self.tags]

    def heat_self(self) -> Optional[int | str]:
        for t in self.tags:
            if t.get("id") == "tg_heat_self":
                return t.get("val", 1)
        return None


@dataclass
class SystemAction:
    name: str
    activation: str     # Protocol / Quick / Full / Reaction / Invade / etc.
    detail: str
    damage: list[dict]  # [{type, val, ap?, target?, aoe?}, ...]
    range_data: list[dict]
    frequency: str


@dataclass
class System:
    id: str
    name: str
    sp: int
    tags: list[dict]    # raw tag objects
    effect: str
    type: str
    description: str
    actions: list[SystemAction]
    source: str
    license_name: str

    @property
    def tag_ids(self) -> list[str]:
        return [t.get("id", "") for t in self.tags]

    def heat_self(self) -> Optional[int | str]:
        for t in self.tags:
            if t.get("id") == "tg_heat_self":
                return t.get("val", 1)
        return None


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

def _clean_html(text: str) -> str:
    """Strip basic HTML tags from comp/con description strings."""
    import re
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


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
        tags=data.get("tags", []),
        sp=data.get("sp", 0),
        effect=_clean_html(data.get("effect", "") or ""),
        description=_clean_html(data.get("description", "") or ""),
        on_hit=_clean_html(data.get("on_hit", "") or ""),
        source=data.get("source", ""),
        license_name=data.get("license", ""),
    )


def _parse_action(a: dict) -> SystemAction:
    return SystemAction(
        name=a.get("name", ""),
        activation=a.get("activation", ""),
        detail=_clean_html(a.get("detail", "") or ""),
        damage=a.get("damage", []),
        range_data=a.get("range", []),
        frequency=a.get("frequency", ""),
    )


def _parse_system(s: dict) -> System:
    data = s.get("data", {})
    actions = [_parse_action(a) for a in data.get("actions", [])]
    return System(
        id=s.get("id", ""),
        name=data.get("name", "Unknown System"),
        sp=data.get("sp", 0),
        tags=data.get("tags", []),
        effect=_clean_html(data.get("effect", "") or ""),
        type=data.get("type", ""),
        description=_clean_html(data.get("description", "") or ""),
        actions=actions,
        source=data.get("source", ""),
        license_name=data.get("license", ""),
    )


def _parse_mech_stats(raw: dict) -> MechStats:
    mx = raw.get("max", {})
    cu = raw.get("current", {})

    def c(key):
        val = cu.get(key)
        if val is None:
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
    idx = raw.get("active_loadout_index", 0)
    loadouts = raw.get("loadouts") or [{}]
    loadout = loadouts[min(idx, len(loadouts) - 1)]

    weapons: list[Weapon] = []
    for mount in loadout.get("mounts", []):
        for slot in mount.get("slots", []):
            w = _parse_weapon(slot)
            if w:
                weapons.append(w)
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

    talents = []
    for t in pilot_data.get("talents", []):
        td = t.get("data", {})
        talents.append(PilotTalent(
            id=t.get("id", ""),
            name=td.get("name", "?"),
            terse=td.get("terse", ""),
            rank=t.get("rank", 1),
        ))

    licenses = [
        (lic.get("id", "?"), lic.get("rank", 0))
        for lic in pilot_data.get("licenses", [])
    ]

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