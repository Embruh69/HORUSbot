"""
Simple in-memory store for imported Lancer characters.
Maps (guild_id, user_id) → LancerCharacter.
This is wiped on bot restart — a real deployment would swap this
for a SQLite/Postgres/JSON-file backend.
"""
from __future__ import annotations
from typing import Optional
from utils.parser import LancerCharacter

# { (guild_id, user_id): LancerCharacter }
_store: dict[tuple[int, int], LancerCharacter] = {}


def save(guild_id: int, user_id: int, char: LancerCharacter) -> None:
    _store[(guild_id, user_id)] = char


def load(guild_id: int, user_id: int) -> Optional[LancerCharacter]:
    return _store.get((guild_id, user_id))


def delete(guild_id: int, user_id: int) -> bool:
    key = (guild_id, user_id)
    if key in _store:
        del _store[key]
        return True
    return False
