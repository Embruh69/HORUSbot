"""
Lancer comp/con JSON parser.
Turns a raw "Save Pilot" export into clean Python dataclasses.

Fixes in this version
──────────────────────
Bug 1: Flex mount `extra` slots were not parsed — second weapon on a Flex
       mount is stored in mount.extra[], not mount.slots[].
Bug 2: Mech stats stored as all-zero when comp/con hasn't computed them yet.
       Stats are now computed from frameData.stats + mechSkills + grit when
       the stored max values are zero.
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
    family: str
    rank: int

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
    weapon_type: str
    mount_size: str
    damage: list[dict]
    range_data: list[dict]
    tags: list[dict]
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
    activation: str
    detail: str = ""
    damage: list[dict] = field(default_factory=list)
    range_data: list[dict] = field(default_factory=list)
    frequency: str = ""
    trigger: str = ""


@dataclass
class System:
    id: str
    name: str
    sp: int
    tags: list[dict]
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
class FrameTrait:
    name: str
    description: str
    actions: list[SystemAction] = field(default_factory=list)


@dataclass
class CorePower:
    name: str
    description: str
    passive_name: str = ""
    passive_effect: str = ""
    active_name: str = ""
    active_effect: str = ""
    activation: str = ""
    deactivation: str = ""
    use: str = ""


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
    traits: list[FrameTrait] = field(default_factory=list)
    core_power: CorePower | None = None


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
        trigger=a.get("trigger", ""),
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


def _compute_mech_stats(
    stored_max: dict,
    frame_stats: dict,
    hull: int,
    agility: int,
    systems: int,
    engineering: int,
    grit: int,
) -> dict:
    """
    Compute mech stats from frameData.stats + mechSkills + grit.

    Called when stored_max values are all zero (comp/con didn't pre-compute them).
    Also called to fill in any individual stat that is zero/missing in stored_max.

    Lancer formulas:
        hp           = frame_hp + (2 * hull)
        heatcap      = frame_heatcap + engineering
        evasion      = frame_evasion + agility
        edef         = frame_edef + systems
        save_target  = frame_save + grit
        speed        = frame_speed          (no bonus)
        sensor_range = frame_sensor_range   (no bonus)
        repair_cap   = frame_repcap + hull
        sp           = frame_sp + systems
        structure    = frame_structure      (no bonus from skills)
        stress       = frame_stress
        size         = frame_size
        armor        = frame_armor
    """
    def _f(key: str, default: int = 0) -> int:
        return int(frame_stats.get(key, default) or 0)

    computed = {
        "hp":            _f("hp")            + 2 * hull,
        "heatcap":       _f("heatcap")       + engineering,
        "evasion":       _f("evasion")       + agility,
        "edef":          _f("edef")          + systems,
        "saveTarget":    _f("save")          + grit,
        "speed":         _f("speed"),
        "sensorRange":   _f("sensor_range"),
        "repairCapacity":_f("repcap")        + hull,
        "sp":            _f("sp")            + systems,
        "structure":     _f("structure"),
        "stress":        _f("stress"),
        "size":          _f("size", 1),
        "armor":         _f("armor"),
    }

    # Merge: use stored value if non-zero, computed otherwise
    result = {}
    for k, computed_val in computed.items():
        stored = stored_max.get(k) or 0
        result[k] = stored if stored != 0 else computed_val

    return result


def _parse_mech_stats(
    raw: dict,
    frame_stats: dict | None = None,
    hull: int = 0,
    agility: int = 0,
    systems: int = 0,
    engineering: int = 0,
    grit: int = 0,
) -> MechStats:
    mx = raw.get("max", {})
    cu = raw.get("current", {})

    # If all stored max stats are zero, compute from frameData
    key_stats = ("hp", "structure", "stress", "evasion", "edef")
    all_zero = all((mx.get(k) or 0) == 0 for k in key_stats)

    if all_zero and frame_stats:
        mx = _compute_mech_stats(mx, frame_stats, hull, agility, systems, engineering, grit)

    def c(key):
        val = cu.get(key)
        if val is None:
            val = mx.get(key, 0)
        return val or 0

    def m(key, default=0):
        return mx.get(key, default) or default

    return MechStats(
        hp=m("hp"),
        armor=m("armor"),
        structure=m("structure", 1),
        stress=m("stress", 1),
        heatcap=m("heatcap") or 10,
        speed=m("speed"),
        evasion=m("evasion"),
        edef=m("edef"),
        sensor_range=m("sensorRange"),
        save_target=m("saveTarget"),
        repair_capacity=m("repairCapacity"),
        sp=m("sp"),
        size=m("size", 1),
        current_hp=c("hp"),
        current_heat=c("heat"),
        current_structure=c("structure") or m("structure", 1),
        current_stress=c("stress") or m("stress", 1),
    )


def _parse_mech(raw: dict, favorite_id: Optional[str], pilot_skills: dict) -> Mech:
    """
    Parse a single mech.

    pilot_skills: dict with keys hull, agility, systems, engineering, grit
                  used to compute stats when stored values are zero.
    """
    fd  = raw.get("frameData", {})
    idx = raw.get("active_loadout_index", 0)
    loadouts = raw.get("loadouts") or [{}]
    loadout  = loadouts[min(idx, len(loadouts) - 1)]

    # ── Frame traits ──────────────────────────────────────────────────────────
    traits = []
    for t in fd.get("traits", []):
        actions = [
            SystemAction(
                name=a.get("name", ""),
                activation=a.get("activation", ""),
                trigger=a.get("trigger", ""),
                detail=a.get("detail", ""),
                damage=a.get("damage", []),
                range_data=a.get("range", []),
                frequency=a.get("frequency", ""),
            )
            for a in t.get("actions", [])
        ]
        traits.append(FrameTrait(
            name=t.get("name", ""),
            description=_clean_html(t.get("description", "")),
            actions=actions,
        ))

    # ── Core power ────────────────────────────────────────────────────────────
    core_json = fd.get("core_system")
    core_power = None
    if core_json:
        core_power = CorePower(
            name=core_json.get("name", ""),
            description=_clean_html(core_json.get("description", "")),
            passive_name=core_json.get("passive_name", ""),
            passive_effect=_clean_html(core_json.get("passive_effect", "")),
            active_name=core_json.get("active_name", ""),
            active_effect=_clean_html(core_json.get("active_effect", "")),
            activation=core_json.get("activation", ""),
            deactivation=core_json.get("deactivation", ""),
            use=core_json.get("use", ""),
        )

    # ── Weapons ───────────────────────────────────────────────────────────────
    # BUG FIX: Flex mounts store the primary weapon in mount.slots[] and the
    # secondary Aux slot in mount.extra[].  Both must be parsed.
    weapons: list[Weapon] = []

    for mount in loadout.get("mounts", []):
        # Primary slots
        for slot in mount.get("slots", []):
            w = _parse_weapon(slot)
            if w:
                weapons.append(w)
        # Extra slots (Flex mount secondary, or other mount extras)
        for slot in mount.get("extra", []):
            w = _parse_weapon(slot)
            if w:
                weapons.append(w)

    # Integrated mounts
    for im in loadout.get("integratedMounts", []):
        for slot in im.get("slots", []):
            w = _parse_weapon(slot)
            if w:
                weapons.append(w)

    # ── Systems ───────────────────────────────────────────────────────────────
    systems = [_parse_system(s) for s in loadout.get("systems", [])]
    systems += [_parse_system(s) for s in loadout.get("integratedSystems", [])]

    # ── Stats ─────────────────────────────────────────────────────────────────
    stats = _parse_mech_stats(
        raw.get("stats", {}),
        frame_stats=fd.get("stats"),
        **pilot_skills,
    )

    return Mech(
        id=raw.get("id", ""),
        name=raw.get("name", "Unnamed Mech"),
        frame_id=raw.get("frame", ""),
        frame_name=fd.get("name", raw.get("frame", "Unknown Frame")),
        frame_source=fd.get("source", "?"),
        frame_mechtype=fd.get("mechtype", []),
        stats=stats,
        weapons=weapons,
        systems=systems,
        is_active=(raw.get("id") == favorite_id),
        traits=traits,
        core_power=core_power,
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
            "Export your pilot from comp/con using **Export -> Save Pilot**."
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

    core_bonuses  = [cb.get("id", "") for cb in pilot_data.get("core_bonuses", [])]
    mech_skills   = pilot_data.get("mechSkills", [0, 0, 0, 0])
    pilot_stats   = _parse_pilot_stats(pilot_data.get("stats", {}), mech_skills)

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

    # Build pilot_skills dict for mech stat computation
    level     = pilot_data.get("level", 0)
    grit      = (level // 2) + 1 if level > 0 else 1
    hull, agi, sys_sk, eng = (mech_skills + [0, 0, 0, 0])[:4]
    pilot_skills_dict = dict(
        hull=hull, agility=agi, systems=sys_sk,
        engineering=eng, grit=grit,
    )

    favorite_id = pilot_data.get("favorite_mech")
    mechs = [
        _parse_mech(m, favorite_id, pilot_skills_dict)
        for m in pilot_data.get("mechs", [])
    ]

    return LancerCharacter(pilot=pilot, mechs=mechs)
