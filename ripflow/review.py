"""`ripflow review <folder>`: go through an existing library album by album, attended.

Each album is processed like `process --ask`: you see a before/after table and answer y (go
ahead), n (wrong release: pick another) or s (skip). The answer is remembered in the album's
ripflow.json under "review", so a later run carries on where this one stopped:

    "review": {"result": "applied", "date": "2026-11-23", "release": "<MusicBrainz id>"}

"applied" and "unchanged" (nothing needed changing, so nothing was asked) albums are never
reviewed again unless --include-completed; "skipped" ones only with --include-skipped. The
record goes into the source folder (so the next run skips it) and, when the album was filed,
into the new folder too (so it survives moving the new folders back over the old ones).
"""
from __future__ import annotations

import os
from pathlib import Path

from . import albumstate
from .config import log
from .library import SYSTEM
from .pipeline import process_folder
from .prompt import ask_release
from .util import natural_key

KEY = "review"


def find_albums(root: Path) -> tuple[list[Path], list[Path]]:
    """(album folders, folders whose own FLAC files were ignored). An album folder holds FLAC
    files and has no folder below it that does; a folder with FLAC files *and* album folders
    below it is an artist folder, and its loose files are not part of any album."""
    with_flac: list[Path] = []
    for here, dirs, names in os.walk(root):
        dirs[:] = sorted((d for d in dirs if d.lower() not in SYSTEM), key=natural_key)
        if any(n.lower().endswith(".flac") for n in names):
            with_flac.append(Path(here))
    albums, loose = [], []
    for d in with_flac:
        below = any(o != d and d in o.parents for o in with_flac)
        (loose if below else albums).append(d)
    return albums, loose


def _record(folder: Path, entry: dict) -> None:
    if folder.is_dir():
        albumstate.save(folder, {**albumstate.load(folder), KEY: entry})


def review(cfg: dict, root: Path, *, move: bool = False, include_skipped: bool = False,
           include_completed: bool = False, limit: int = 0,
           no_cover: bool = False, ignore_local_art: bool = False, only_local_art: bool = False,
           show_art: bool = False, prefer_discid: bool = False) -> int:
    root = Path(root)
    if not root.is_dir():
        raise SystemExit(f"not a folder: {root}")
    albums, loose = find_albums(root)
    for d in loose:
        print(f"note: FLAC files directly in {d} are not part of an album folder - ignored")
    todo, done = [], {"applied": 0, "skipped": 0}
    for d in albums:
        result = (albumstate.load(d).get(KEY) or {}).get("result")
        if (result in ("applied", "unchanged") and not include_completed) or \
                (result == "skipped" and not include_skipped):
            done["skipped" if result == "skipped" else "applied"] += 1
        else:
            todo.append(d)
    print(f"{len(albums)} album folder(s) under {root}: {len(todo)} to review, "
          f"{done['applied']} done before (applied or unchanged), {done['skipped']} skipped before"
          + ("" if include_skipped or not done["skipped"] else " (--include-skipped to see those again)")
          + ("" if include_completed or not done["applied"] else " (--include-completed for the done ones)"))
    if limit:
        todo = todo[:limit]
    tally = {"applied": 0, "unchanged": 0, "skipped": 0, "has non-FLAC audio": 0, "not finished": 0}
    try:
        for n, d in enumerate(todo, 1):
            print(f"\n{'=' * 78}\n[{n}/{len(todo)}] {d.relative_to(root) if d != root else d}")
            outcomes = process_folder(cfg, d, move=move, no_cover=no_cover, ignore_local_art=ignore_local_art,
                                      only_local_art=only_local_art, chooser=ask_release, confirm=True,
                                      show_art=show_art, prefer_discid=prefer_discid)
            for o in outcomes:
                if o.decision:
                    entry = {"result": o.decision, "date": albumstate.today(), "release": o.release}
                    _record(o.source, entry)
                    if o.decision == "applied" and o.dest:
                        _record(o.dest, entry)
                    tally[o.decision] += 1
                elif any("non-FLAC audio" in p for p in o.problems):
                    tally["has non-FLAC audio"] += 1
                    print(f"  left alone: {o.problems[0]}")
                    print("  not recorded - it will come up again once the folder holds only FLAC")
                    continue
                else:
                    tally["not finished"] += 1
                    for p in o.problems:
                        print(f"  attention: {p}")
                    print("  not recorded - it will come up again next time")
            if any("unreachable" in p for o in outcomes for p in o.problems):
                print("\nMusicBrainz is unreachable, so the review stops here. Run it again later to carry on.")
                break
    except KeyboardInterrupt:
        print("\n\nStopped. Albums you answered are remembered; run review again to carry on.")
        _summary(tally)
        return 130
    _summary(tally)
    return 0 if not (tally["not finished"] or tally["has non-FLAC audio"]) else 2


def _summary(tally: dict) -> None:
    print("\n== Review summary ==")
    for k, v in tally.items():
        print(f"  {v:5}  {k}")
    log.info("review: %s", ", ".join(f"{v} {k}" for k, v in tally.items()))
