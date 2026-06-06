# utils/tags.py

import json
from pathlib import Path

TAG_FILE = Path(__file__).resolve().parent.parent / "data" / "tags.json"

with open(TAG_FILE, encoding="utf-8") as f:
    _raw = json.load(f)

TAGS = {t["id"]: t for t in _raw}


def format_tag(tag_obj: dict) -> str:
    """
    Convert:
        {"id": "tg_overkill", "val": 1}
    into:
        "Overkill 1"
    """

    tag_id = tag_obj.get("id", "")
    val = tag_obj.get("val")

    tag_data = TAGS.get(tag_id)

    if not tag_data:
        return tag_id

    name = tag_data.get("name", tag_id)

    if "{VAL}" in name:
        if val is not None:
            return name.replace("{VAL}", str(val))
        return name.replace("{VAL}", "").strip()

    return name