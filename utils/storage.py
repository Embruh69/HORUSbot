"""
SQLite-backed character store for the Lancer bot.

Schema
------
characters
  guild_id   INTEGER  — Discord guild snowflake
  user_id    INTEGER  — Discord user snowflake
  callsign   TEXT     — for quick display / listing
  raw_json   TEXT     — the full comp/con export JSON (re-parsed on load)
  imported_at TEXT    — ISO-8601 timestamp

The raw comp/con JSON is stored verbatim so we never lose data and can
re-parse whenever the parser is updated.
"""
from __future__ import annotations
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from utils.parser import LancerCharacter, parse_compcon_json

# ── config ────────────────────────────────────────────────────────────────────

DB_PATH = Path(__file__).parent.parent / "data" / "characters.db"

# ── internal helpers ──────────────────────────────────────────────────────────

_local = threading.local()   # one connection per thread


def _conn() -> sqlite3.Connection:
    """Return (or create) the per-thread connection."""
    if not hasattr(_local, "conn"):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(DB_PATH, check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")   # safer concurrent writes
        con.execute("PRAGMA foreign_keys=ON")
        _create_tables(con)
        _local.conn = con
    return _local.conn


def _create_tables(con: sqlite3.Connection) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS characters (
            guild_id    INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            callsign    TEXT    NOT NULL,
            raw_json    TEXT    NOT NULL,
            imported_at TEXT    NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        )
    """)
    con.commit()


# ── public API ────────────────────────────────────────────────────────────────

def save(guild_id: int, user_id: int, char: LancerCharacter, raw_json: str) -> None:
    """
    Persist a character.  Pass the original raw JSON string so we can
    round-trip it perfectly without re-serialising our dataclasses.
    """
    now = datetime.now(timezone.utc).isoformat()
    con = _conn()
    con.execute(
        """
        INSERT INTO characters (guild_id, user_id, callsign, raw_json, imported_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (guild_id, user_id) DO UPDATE SET
            callsign    = excluded.callsign,
            raw_json    = excluded.raw_json,
            imported_at = excluded.imported_at
        """,
        (guild_id, user_id, char.pilot.callsign, raw_json, now),
    )
    con.commit()


def load(guild_id: int, user_id: int) -> Optional[LancerCharacter]:
    """Load and parse a character; returns None if not found."""
    row = _conn().execute(
        "SELECT raw_json FROM characters WHERE guild_id=? AND user_id=?",
        (guild_id, user_id),
    ).fetchone()
    if row is None:
        return None
    return parse_compcon_json(row["raw_json"])


def delete(guild_id: int, user_id: int) -> bool:
    """Delete a character; returns True if a row was removed."""
    con = _conn()
    cur = con.execute(
        "DELETE FROM characters WHERE guild_id=? AND user_id=?",
        (guild_id, user_id),
    )
    con.commit()
    return cur.rowcount > 0


def list_guild(guild_id: int) -> list[dict]:
    """
    Return a list of dicts with 'user_id', 'callsign', 'imported_at'
    for every character registered in a guild.  Useful for future
    admin/listing commands.
    """
    rows = _conn().execute(
        "SELECT user_id, callsign, imported_at FROM characters WHERE guild_id=? ORDER BY callsign",
        (guild_id,),
    ).fetchall()
    return [dict(r) for r in rows]
