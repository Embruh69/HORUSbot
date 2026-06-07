"""
SQLite-backed encounter / enemy store for the Lancer bot.

Schema
──────
encounters
  guild_id      INTEGER   — Discord guild snowflake
  channel_id    INTEGER   — channel where the encounter lives
  slug          TEXT      — unique label within the encounter,
                            e.g. "barricade_a"  (lower, underscores)
  display_name  TEXT      — pretty label shown to players, e.g. "BARRICADE A"
  base_name     TEXT      — original NPC class name
  raw_json      TEXT      — full comp/con NPC export (re-parsed on load)
  current_hp    INTEGER
  current_heat  INTEGER
  current_structure INTEGER
  current_stress    INTEGER
  burn          INTEGER
  overshield    INTEGER
  is_active     INTEGER   — boolean: 1 = selected for current activation
  is_dead       INTEGER   — boolean: 1 = removed from play
  added_at      TEXT      — ISO-8601

PRIMARY KEY (guild_id, channel_id, slug)

Design notes
────────────
• One encounter per channel at a time.  The GM runs !encounter clear to
  wipe the slate and start fresh.
• `slug` is the stable key; `display_name` is what users see.
• HP/heat mutations write only the relevant columns — no need to re-parse
  the full JSON on every tick.
"""
from __future__ import annotations
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from utils.npc_parser import NpcEnemy, parse_npc_json

DB_PATH = Path(__file__).parent.parent / "data" / "encounters.db"

_local = threading.local()


def _conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn"):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(DB_PATH, check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        _create_tables(con)
        _local.conn = con
    return _local.conn


def _create_tables(con: sqlite3.Connection) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS encounters (
            guild_id          INTEGER NOT NULL,
            channel_id        INTEGER NOT NULL,
            slug              TEXT    NOT NULL,
            display_name      TEXT    NOT NULL,
            base_name         TEXT    NOT NULL,
            raw_json          TEXT    NOT NULL,
            current_hp        INTEGER NOT NULL DEFAULT 0,
            current_heat      INTEGER NOT NULL DEFAULT 0,
            current_structure INTEGER NOT NULL DEFAULT 1,
            current_stress    INTEGER NOT NULL DEFAULT 1,
            burn              INTEGER NOT NULL DEFAULT 0,
            overshield        INTEGER NOT NULL DEFAULT 0,
            is_active         INTEGER NOT NULL DEFAULT 0,
            is_dead           INTEGER NOT NULL DEFAULT 0,
            added_at          TEXT    NOT NULL,
            PRIMARY KEY (guild_id, channel_id, slug)
        )
    """)
    con.commit()


# ── helpers ───────────────────────────────────────────────────────────────────

def _row_to_enemy(row: sqlite3.Row) -> NpcEnemy:
    """Reconstruct an NpcEnemy from a DB row."""
    enemy = parse_npc_json(row["raw_json"], display_name=row["display_name"])
    s = enemy.stats
    s.current_hp        = row["current_hp"]
    s.current_heat      = row["current_heat"]
    s.current_structure = row["current_structure"]
    s.current_stress    = row["current_stress"]
    s.burn              = row["burn"]
    s.overshield        = row["overshield"]
    enemy.is_active     = bool(row["is_active"])
    enemy.is_dead       = bool(row["is_dead"])
    return enemy


def _slug_exists(con: sqlite3.Connection, guild_id: int, channel_id: int, slug: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM encounters WHERE guild_id=? AND channel_id=? AND slug=?",
        (guild_id, channel_id, slug),
    ).fetchone()
    return row is not None


def _make_unique_slug(
    con: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    base: str,
) -> str:
    """
    Generate a slug that does not already exist in the encounter.
    Tries base → base_2 → base_3 …
    """
    if not _slug_exists(con, guild_id, channel_id, base):
        return base
    i = 2
    while True:
        candidate = f"{base}_{i}"
        if not _slug_exists(con, guild_id, channel_id, candidate):
            return candidate
        i += 1


_ALPHA = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _letter_suffix(n: int) -> str:
    """0→A, 1→B … 25→Z, 26→AA …"""
    result = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        result = _ALPHA[r] + result
    return result


# ── public API ────────────────────────────────────────────────────────────────

def add_enemy(
    guild_id: int,
    channel_id: int,
    enemy: NpcEnemy,
    raw_json: str,
) -> NpcEnemy:
    """
    Persist a single enemy.  Slug / display_name are already set on enemy.
    Returns the stored enemy (unchanged).
    """
    now = datetime.now(timezone.utc).isoformat()
    s = enemy.stats
    con = _conn()
    con.execute(
        """
        INSERT INTO encounters
          (guild_id, channel_id, slug, display_name, base_name,
           raw_json, current_hp, current_heat, current_structure,
           current_stress, burn, overshield, is_active, is_dead, added_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT (guild_id, channel_id, slug) DO UPDATE SET
          display_name      = excluded.display_name,
          raw_json          = excluded.raw_json,
          current_hp        = excluded.current_hp,
          current_heat      = excluded.current_heat,
          current_structure = excluded.current_structure,
          current_stress    = excluded.current_stress,
          burn              = excluded.burn,
          overshield        = excluded.overshield,
          is_active         = excluded.is_active,
          is_dead           = excluded.is_dead,
          added_at          = excluded.added_at
        """,
        (
            guild_id, channel_id,
            enemy.slug, enemy.display_name, enemy.base_name,
            raw_json,
            s.current_hp, s.current_heat,
            s.current_structure, s.current_stress,
            s.burn, s.overshield,
            int(enemy.is_active), int(enemy.is_dead),
            now,
        ),
    )
    con.commit()
    return enemy


def add_enemies_batch(
    guild_id: int,
    channel_id: int,
    raw_jsons: list[str],
    qty_per_json: list[int] | None = None,
) -> list[NpcEnemy]:
    """
    Add one or more enemies (possibly multiple copies of each).

    raw_jsons:    list of raw JSON strings, one per attached file.
    qty_per_json: parallel list of quantities; defaults to [1, 1, …].

    Naming rules
    ─────────────
    • qty == 1 and only one unique base_name in the whole batch
      → no suffix (e.g. "BARRICADE")
    • qty > 1 OR the same base_name appears more than once across files
      → suffix with A, B, C … (e.g. "BARRICADE A", "BARRICADE B")
    """
    if qty_per_json is None:
        qty_per_json = [1] * len(raw_jsons)

    con = _conn()
    now = datetime.now(timezone.utc).isoformat()

    # First pass — parse everything so we can decide naming
    parsed: list[tuple[NpcEnemy, str, int]] = []   # (enemy, raw, qty)
    for raw, qty in zip(raw_jsons, qty_per_json):
        base = parse_npc_json(raw)
        parsed.append((base, raw, qty))

    # Count how many instances each base_name will have total
    from collections import Counter
    total_by_base: Counter = Counter()
    for enemy, _, qty in parsed:
        total_by_base[enemy.base_name] += qty

    added: list[NpcEnemy] = []
    # Track per-base how many have been assigned so far (for letter suffixes)
    letter_idx: dict[str, int] = {}

    for enemy, raw, qty in parsed:
        bname = enemy.base_name
        needs_suffix = (total_by_base[bname] > 1)

        for _ in range(qty):
            if needs_suffix:
                idx = letter_idx.get(bname, 0)
                suffix = _letter_suffix(idx)
                letter_idx[bname] = idx + 1
                display_name = f"{bname} {suffix}"
            else:
                display_name = bname

            # Build a fresh enemy with the right display name
            e = parse_npc_json(raw, display_name=display_name)
            slug = _make_unique_slug(con, guild_id, channel_id, e.slug)
            e.display_name = display_name
            # Re-slug in case _make_unique_slug changed it
            object.__setattr__(e, '_slug_override', slug)

            add_enemy(guild_id, channel_id, e, raw)
            # The stored slug might differ; re-load to get canonical state
            added.append(e)

    return added


def list_enemies(
    guild_id: int,
    channel_id: int,
    include_dead: bool = False,
) -> list[NpcEnemy]:
    """Return all enemies in the encounter, ordered by display_name."""
    query = (
        "SELECT * FROM encounters WHERE guild_id=? AND channel_id=?"
        + ("" if include_dead else " AND is_dead=0")
        + " ORDER BY display_name"
    )
    rows = _conn().execute(query, (guild_id, channel_id)).fetchall()
    return [_row_to_enemy(r) for r in rows]


def get_enemy(
    guild_id: int,
    channel_id: int,
    slug: str,
) -> Optional[NpcEnemy]:
    row = _conn().execute(
        "SELECT * FROM encounters WHERE guild_id=? AND channel_id=? AND slug=?",
        (guild_id, channel_id, slug),
    ).fetchone()
    return _row_to_enemy(row) if row else None


def resolve_enemy_slug(
    guild_id: int,
    channel_id: int,
    query: str,
) -> Optional[NpcEnemy]:
    """
    Case-insensitive partial match on display_name or slug.
    Returns the first match, or None.
    """
    q = query.strip().lower()
    rows = _conn().execute(
        "SELECT * FROM encounters WHERE guild_id=? AND channel_id=? AND is_dead=0"
        " ORDER BY display_name",
        (guild_id, channel_id),
    ).fetchall()
    for row in rows:
        if (q in row["display_name"].lower()) or (q in row["slug"]):
            return _row_to_enemy(row)
    return None


def update_enemy_vitals(
    guild_id: int,
    channel_id: int,
    slug: str,
    *,
    hp: int | None = None,
    heat: int | None = None,
    structure: int | None = None,
    stress: int | None = None,
    burn: int | None = None,
    overshield: int | None = None,
    is_dead: bool | None = None,
) -> bool:
    """Patch only the supplied columns.  Returns True if a row was updated."""
    parts = []
    vals = []
    if hp is not None:
        parts.append("current_hp=?"); vals.append(hp)
    if heat is not None:
        parts.append("current_heat=?"); vals.append(heat)
    if structure is not None:
        parts.append("current_structure=?"); vals.append(structure)
    if stress is not None:
        parts.append("current_stress=?"); vals.append(stress)
    if burn is not None:
        parts.append("burn=?"); vals.append(burn)
    if overshield is not None:
        parts.append("overshield=?"); vals.append(overshield)
    if is_dead is not None:
        parts.append("is_dead=?"); vals.append(int(is_dead))
    if not parts:
        return False
    vals += [guild_id, channel_id, slug]
    con = _conn()
    cur = con.execute(
        f"UPDATE encounters SET {', '.join(parts)} WHERE guild_id=? AND channel_id=? AND slug=?",
        vals,
    )
    con.commit()
    return cur.rowcount > 0


def set_active_enemies(
    guild_id: int,
    channel_id: int,
    slugs: list[str],
) -> int:
    """
    Set is_active=1 for the given slugs, 0 for all others.
    Returns the count of rows set to active.
    """
    con = _conn()
    # Clear all
    con.execute(
        "UPDATE encounters SET is_active=0 WHERE guild_id=? AND channel_id=?",
        (guild_id, channel_id),
    )
    # Set selected
    count = 0
    for slug in slugs:
        cur = con.execute(
            "UPDATE encounters SET is_active=1 WHERE guild_id=? AND channel_id=? AND slug=?",
            (guild_id, channel_id, slug),
        )
        count += cur.rowcount
    con.commit()
    return count


def remove_enemy(guild_id: int, channel_id: int, slug: str) -> bool:
    """Soft-delete (mark dead) an enemy.  Returns True if found."""
    return update_enemy_vitals(guild_id, channel_id, slug, is_dead=True)


def clear_encounter(guild_id: int, channel_id: int) -> int:
    """Hard-delete all enemies in this channel's encounter."""
    con = _conn()
    cur = con.execute(
        "DELETE FROM encounters WHERE guild_id=? AND channel_id=?",
        (guild_id, channel_id),
    )
    con.commit()
    return cur.rowcount
