"""Per-album memory, kept in a small JSON file `ripflow.json` inside the album folder.

It travels with the album (copies, renames, other machines, a fresh clone of ripflow) and
holds facts worth not looking up again - never log output. Current keys:

    {
      "ripflow": 1,
      "apple":          {"album_id": "1440765580", "storefront": "us"},
      "animated_cover": {"result": "no animated cover on Apple Music", "checked": "2026-11-18"},
      "still_cover":    {"result": "no still cover found", "checked": "2026-11-18"}
    }

Unknown keys are preserved, so later versions can add their own.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

FILE = "ripflow.json"
# animated_cover results meaning "Apple Music has none": not looked for again unless asked
NO_ANIMATION = ("no animated cover on Apple Music", "album not found on Apple Music")
VERSION = 1


def load(folder: Path) -> dict:
    try:
        data = json.loads((Path(folder) / FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(folder: Path, data: dict) -> None:
    """Write atomically; does nothing if the content would not change."""
    path = Path(folder) / FILE
    data = {"ripflow": VERSION, **{k: v for k, v in data.items() if k != "ripflow"}}
    if load(folder) == data:
        return
    tmp = path.with_name(FILE + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def update(folder: Path, **sections: dict | None) -> dict:
    """Merge sections into the album's file (a value of None removes that section)."""
    data = load(folder)
    for name, value in sections.items():
        if value is None:
            data.pop(name, None)
        else:
            data[name] = value
    save(folder, data)
    return data


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")
