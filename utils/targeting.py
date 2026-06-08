"""
utils/targeting.py — shared target resolution for player and NPC attacks.

A "target" supplied with -t <name> can be:
  • An NPC in the current encounter  → ("npc",    slug,    NpcEnemy)
  • A mentioned Discord user         → ("player", user_id, None)
  • A player name/callsign in the DB → ("player", user_id, LancerCharacter)
  • Unresolvable                     → None

parse_target_flag(text)
    Strip the -t … flag out of a raw argument string and return
    (cleaned_text, raw_target_query).  raw_target_query is "" if no flag.

resolve_target(guild_id, channel_id, query, guild_members)
    Turn a raw query string into a TargetResult or None.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Literal, Optional

# ── TargetResult ──────────────────────────────────────────────────────────────

@dataclass
class TargetResult:
    kind: Literal["npc", "player"]
    # NPC fields
    npc_slug:    str  = ""
    npc_name:    str  = ""    # display_name for embed labels
    npc_hp:      int  = 0
    npc_max_hp:  int  = 0
    npc_heat:    int  = 0
    npc_max_heat: int = 0
    npc_heatcap: int  = 0
    # Player fields
    player_user_id:  int  = 0
    player_callsign: str  = ""

    @property
    def label(self) -> str:
        if self.kind == "npc":
            return self.npc_name
        return self.player_callsign or f"<@{self.player_user_id}>"


# ── Flag parsing ──────────────────────────────────────────────────────────────

# Matches: -t foo  | -target foo  | --t foo  | --target foo
# Captures everything after the flag until end-of-string or another -flag.
_TARGET_RE = re.compile(
    r"""
    (?:--|-)                  # one or two dashes
    t(?:arget)?               # "t" or "target"
    \s+                       # mandatory space
    (                         # capture group: the target name
        (?!-)                 # must not start with another dash
        .+?                   # non-greedy match
    )
    (?=\s+--|$)              # lookahead: another flag or end of string
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Simpler fallback for "-t name" at end of string
_TARGET_END_RE = re.compile(
    r"""
    (?:--|-)t(?:arget)?\s+
    (.+)$
    """,
    re.VERBOSE | re.IGNORECASE,
)


def parse_target_flag(text: str) -> tuple[str, str]:
    """
    Strip the -t / --target flag from text.

    Returns (cleaned_text, raw_target_query).
    raw_target_query is "" if no flag was found.

    Examples
    --------
    "torch -t barricade a"       → ("torch", "barricade a")
    "graviton lance -t @Player"  → ("graviton lance", "@Player")
    "drag down acc 2 -t barca"   → ("drag down acc 2", "barca")
    "mobile printer"             → ("mobile printer", "")
    """
    # Try the lookahead version first (flag in the middle of args)
    m = _TARGET_RE.search(text)
    if not m:
        # Try end-of-string version
        m = _TARGET_END_RE.search(text)

    if not m:
        return text.strip(), ""

    raw_target = m.group(1).strip()
    cleaned    = (text[:m.start()] + text[m.end():]).strip()
    return cleaned, raw_target


# ── Target resolution ─────────────────────────────────────────────────────────

def resolve_target(
    guild_id:     int,
    channel_id:   int,
    query:        str,
    guild_members: dict[int, str] | None = None,  # {user_id: display_name}
) -> Optional[TargetResult]:
    """
    Resolve a raw target query.

    Priority
    ────────
    1. @mention  → player
    2. NPC fuzzy match in this channel's encounter
    3. Player callsign match in the characters DB (same guild)

    guild_members is passed in from the Discord context so we don't need
    a guild API call inside the utility layer.
    """
    query = query.strip()
    if not query:
        return None

    # ── 1. Discord @mention ───────────────────────────────────────────────────
    mention_m = re.match(r"<@!?(\d+)>", query)
    if mention_m:
        uid = int(mention_m.group(1))
        callsign = _load_player_callsign(guild_id, uid) or ""
        return TargetResult(
            kind="player",
            player_user_id=uid,
            player_callsign=callsign,
        )

    # ── 2. NPC in encounter ───────────────────────────────────────────────────
    try:
        import utils.npc_storage as npc_storage
        enemy = npc_storage.resolve_enemy_slug(guild_id, channel_id, query)
        if enemy:
            s = enemy.stats
            return TargetResult(
                kind="npc",
                npc_slug=enemy.slug,
                npc_name=enemy.display_name,
                npc_hp=s.current_hp,
                npc_max_hp=s.hp,
                npc_heat=s.current_heat,
                npc_heatcap=s.heatcap,
                npc_max_heat=s.heatcap,
            )
    except Exception:
        pass

    # ── 3. Player callsign / display-name match ───────────────────────────────
    uid = _find_player_by_name(guild_id, query)
    if uid:
        callsign = _load_player_callsign(guild_id, uid) or query
        return TargetResult(
            kind="player",
            player_user_id=uid,
            player_callsign=callsign,
        )

    # ── 4. Display-name match in provided guild_members dict ─────────────────
    if guild_members:
        q = query.lower()
        for uid, name in guild_members.items():
            if q in name.lower():
                callsign = _load_player_callsign(guild_id, uid) or name
                return TargetResult(
                    kind="player",
                    player_user_id=uid,
                    player_callsign=callsign,
                )

    return None


# ── Player DB helpers ─────────────────────────────────────────────────────────

def _load_player_callsign(guild_id: int, user_id: int) -> str | None:
    """Return the stored callsign for a player, or None."""
    try:
        import utils.storage as storage
        char = storage.load(guild_id, user_id)
        return char.pilot.callsign if char else None
    except Exception:
        return None


def _find_player_by_name(guild_id: int, query: str) -> int | None:
    """
    Search the characters DB for a pilot whose callsign contains `query`.
    Returns user_id or None.
    """
    try:
        import utils.storage as storage
        rows = storage.list_guild(guild_id)
        q = query.lower()
        for row in rows:
            if q in row["callsign"].lower():
                return row["user_id"]
    except Exception:
        pass
    return None
