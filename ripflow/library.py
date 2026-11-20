"""`ripflow covers <root>`: bring the cover art of an existing library up to date.

For every album folder under the root:
  - animated cover: if there is no animated cover.webp yet, look the album up on Apple Music
    and save its animated cover when it has one
  - still cover: if there is neither cover.jpg nor the alternate still name, find one the
    same way `process` does (local image files, embedded art, Apple, then Cover Art Archive /
    Deezer); nothing found is fine
  - naming: the still is moved to "cover-static.jpg" when an animated cover.webp exists, and
    back to cover.jpg when it does not

Audio files are never modified. What was learned (Apple album id, "Apple has no animated
cover", "no still cover found") is remembered in each album's `ripflow.json` file, so later runs
skip those lookups wherever the library lives (use --recheck to look again).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .animated import Unavailable, animated_cover, motion_stream
from . import albumstate
from .config import log
from .cover import (LEGACY_STILL_STEMS, apple_album_id, find_cover, is_animated, migrate_legacy_names,
                    oversized_animation, park_static_webp, settle_still, write_replacing)
from .flacfile import FlacError, read_info
from .util import NetworkError, natural_key

MUSIC = {".flac", ".mp3", ".m4a", ".aac", ".alac", ".ogg", ".oga", ".opus", ".wma", ".wav",
         ".aif", ".aiff", ".ape", ".wv", ".mpc", ".dsf", ".dff", ".tak"}
NO_ANIMATION = ("no animated cover on Apple Music", "album not found on Apple Music")
VARIOUS = {"various artists", "various", "va"}
SYSTEM = {".git", "@eadir", "#recycle", "$recycle.bin", "system volume information", ".trash", ".trashes"}
NO_STILL = "no still cover found"


def album_dirs(root: Path, still_name: str) -> list[Path]:
    """Folders with a cover.jpg (or the alternate still name), or with at least one music file
    and no folder.jpg (folder.jpg marks an artist folder)."""
    found = []
    for here, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d.lower() not in SYSTEM)   # "...And Justice for All" is not hidden
        names = {f.lower() for f in files}
        has_music = any(os.path.splitext(f)[1].lower() in MUSIC for f in files)
        if ("cover.jpg" in names or still_name.lower() in names or albumstate.FILE in names
                or any(s + ".jpg" in names for s in LEGACY_STILL_STEMS)
                or (has_music and "folder.jpg" not in names)):
            found.append(Path(here))
    return found


def _ffprobe_tags(cfg: dict, path: Path) -> dict[str, str]:
    exe = shutil.which("ffprobe")
    if not exe:
        ffmpeg = shutil.which(cfg["tools"]["ffmpeg"])
        cand = Path(ffmpeg).with_name("ffprobe" + Path(ffmpeg).suffix) if ffmpeg else None
        exe = str(cand) if cand and cand.is_file() else None
    if not exe:
        return {}
    try:
        out = subprocess.run([exe, "-v", "error", "-show_entries", "format_tags", "-of", "json", str(path)],
                             capture_output=True, text=True, errors="replace", timeout=60).stdout
        return {k.upper(): v for k, v in (json.loads(out).get("format", {}).get("tags") or {}).items()}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}


def album_info(cfg: dict, folder: Path, root: Path) -> dict:
    """Artist, album and MusicBrainz ids from the first track's tags; folder names as a fallback
    (the library layout is Artist/Album)."""
    tracks = sorted((p for p in folder.iterdir() if p.suffix.lower() in MUSIC and p.is_file()),
                    key=lambda p: natural_key(p.name))
    tags: dict[str, str] = {}
    flac = None
    if tracks and tracks[0].suffix.lower() == ".flac":
        try:
            info = read_info(tracks[0])
            tags = {k: v[0] for k, v in info.tags.items() if v}
            flac = tracks[0]
        except (FlacError, OSError):
            pass
    elif tracks:
        tags = _ffprobe_tags(cfg, tracks[0])
    get = lambda *keys: next((tags[k].strip() for k in keys if tags.get(k, "").strip()), "")
    at_top = folder.resolve().parent == root.resolve()        # Artist folder holding loose tracks
    artist = get("ALBUMARTIST", "ALBUM ARTIST", "ALBUM_ARTIST", "ARTIST") or (folder.name if at_top else folder.parent.name)
    album = get("ALBUM") or ("" if at_top else folder.name)
    return dict(artist=artist, album=album, ntracks=len(tracks), flac=flac, barcode=get("BARCODE", "UPC"),
                mbid=get("MUSICBRAINZ_ALBUMID", "MUSICBRAINZ ALBUM ID"),
                group_id=get("MUSICBRAINZ_RELEASEGROUPID", "MUSICBRAINZ RELEASE GROUP ID"),
                various=artist.lower() in VARIOUS or get("COMPILATION") == "1")


def _rate_limited(e: Exception) -> bool:
    return any(code in str(e) for code in ("403", "429"))


def update_covers(cfg: dict, root: Path, *, dry_run: bool = False, recheck: bool = False,
                  limit: int = 0) -> int:
    c = cfg["cover"]
    still_name = c["animated_still_name"]
    try:      # older versions kept one central file; read it once as a fallback, never written
        legacy = json.loads((Path(cfg["paths"]["logs"]) / "covers-state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        legacy = {}
    from . import animated as _animated, cover as _cover
    _cover.stats["itunes"] = _animated.stats["catalog"] = 0
    folders = album_dirs(Path(root), still_name)
    if limit:
        folders = folders[:limit]
    print(f"{len(folders)} album folder(s) under {root}" + ("  (dry run: nothing is written)" if dry_run else ""))
    rows, counts, strikes = [], {}, 0
    stamp = datetime.now().strftime("%Y-%m-%d %H.%M.%S")

    def tally(key: str):
        counts[key] = counts.get(key, 0) + 1

    try:
        for n, folder in enumerate(folders, 1):
            for attempt in (0, 1):
                try:
                    row = _one_album(cfg, folder, Path(root), legacy, dry_run=dry_run, recheck=recheck)
                    strikes = 0
                    break
                except Exception as e:        # one bad album must not stop a library-wide run
                    if isinstance(e, (NetworkError, Unavailable)) and _rate_limited(e) and attempt == 0:
                        log.warning("Apple is limiting requests; waiting 60 s before retrying %s", folder.name)
                        time.sleep(60)
                        continue
                    row = dict(artist="", album=folder.name, animated=f"error: {e}", still="")
                    log.debug("error on %s", folder, exc_info=True)
                    strikes = strikes + 1 if _rate_limited(e) else 0
                    break
            log.info("[%d/%d] %s / %s: animated %s; still %s", n, len(folders), row["artist"] or "?",
                     row["album"], row["animated"], row["still"])
            rows.append((folder, row))
            tally(row["animated"].split(" (")[0].split(":")[0])
            if strikes >= 3:
                log.warning("Apple keeps refusing requests; stopping. Re-run later - finished albums are skipped.")
                break
    except KeyboardInterrupt:
        print("\nstopped - re-run to continue; finished albums are skipped")
    report = Path(cfg["paths"]["logs"]) / "reports" / f"{stamp} covers.tsv"
    report.parent.mkdir(parents=True, exist_ok=True)
    with open(report, "w", encoding="utf-8", newline="\n") as f:
        f.write("folder\tartist\talbum\tanimated\tstill\n")
        f.writelines(f"{d}\t{r['artist']}\t{r['album']}\t{r['animated']}\t{r['still']}\n" for d, r in rows)
    print("\n== Summary ==")
    print(f"Apple calls this run: {_cover.stats['itunes']} iTunes search(es) (spaced {c['itunes_interval']:g} s), "
          f"{_animated.stats['catalog']} Apple Music lookup(s) (spaced {c['apple_music_interval']:g} s)")
    for key, num in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"{num:6d}  animated {key}")
    print(f"\nPer-album results: {report}")
    return 1 if any(r["animated"].startswith("error") for _, r in rows) else 0


def _memory(folder: Path, legacy: dict) -> dict:
    """The album's ripflow.json contents; entries from the old central file are carried over."""
    mem = albumstate.load(folder)
    old = legacy.get(str(folder.resolve()))
    if old and "animated_cover" not in mem and old.get("result"):
        mem["animated_cover"] = {"result": old["result"], "checked": old.get("date", "")}
    if old and "still_cover" not in mem and old.get("still"):
        mem["still_cover"] = {"result": old["still"], "checked": old.get("date", "")}
    return mem


def _one_album(cfg: dict, folder: Path, root: Path, legacy: dict, *, dry_run: bool, recheck: bool) -> dict:
    c = cfg["cover"]
    info = album_info(cfg, folder, root)
    row = dict(artist=info["artist"], album=info["album"], animated="", still="")
    if not info["album"]:
        row["animated"] = row["still"] = "skipped (no album name)"
        return row
    renamed = migrate_legacy_names(folder, cfg, dry_run=dry_run)
    parked = park_static_webp(folder, cfg, dry_run=dry_run)
    has_still = any((folder / n).is_file() for n in ("cover.jpg", c["animated_still_name"],
                                                        *(s + ".jpg" for s in LEGACY_STILL_STEMS)))
    before = json.dumps(albumstate.load(folder), sort_keys=True)   # what is on disk now
    mem = _memory(folder, legacy)
    seen_still, seen_anim = mem.get("still_cover", {}), mem.get("animated_cover", {})
    storefront = c["itunes_country"].lower()
    known_id = None if recheck else (mem.get("apple") or {}).get("album_id") \
        if (mem.get("apple") or {}).get("storefront") == storefront else None
    found: dict = {}

    # still cover, only where there is none (looked up first: it also finds the Apple album)
    cover = None
    if not has_still and c["enabled"] and not recheck and seen_still.get("result") == NO_STILL:
        row["still"] = f"skipped ({NO_STILL} on {seen_still.get('checked', '?')})"
    elif not has_still and c["enabled"]:
        cover, _ = find_cover(cfg, artist=info["artist"], album=info["album"], various=info["various"],
                              ntracks=info["ntracks"], mbid=info["mbid"], group_id=info["group_id"],
                              local_dirs=[folder], embedded_from=info["flac"], found=found, barcode=info["barcode"])
        row["still"] = f"found ({cover.source}, {cover.width}x{cover.height})" if cover else NO_STILL
        mem["still_cover"] = {"result": "found" if cover else NO_STILL, "checked": albumstate.today()}

    # animated cover (with dry_run a still cover.webp is still there, but would be moved aside)
    too_big = oversized_animation(folder / "cover.webp", cfg) if c["animated"] else 0
    if is_animated(folder / "cover.webp") and not too_big:
        row["animated"] = "already there"
    elif not c["animated"]:
        row["animated"] = "off in config"
    elif not too_big and not recheck and seen_anim.get("result") in NO_ANIMATION:
        row["animated"] = f"skipped ({seen_anim['result']} on {seen_anim.get('checked', '?')})"
    else:
        apple_id = found.get("apple_album_id") or known_id or apple_album_id(
            cfg, artist=info["artist"], album=info["album"], various=info["various"], ntracks=info["ntracks"],
            barcode=info["barcode"])
        if apple_id:
            mem["apple"] = {"album_id": apple_id, "storefront": storefront}
        was = f" (was {too_big:.1f} MB)" if too_big else ""
        if not apple_id:
            row["animated"] = NO_ANIMATION[1] + (f"; kept the existing {too_big:.1f} MB one" if too_big else "")
        elif dry_run:
            url = motion_stream(c["itunes_country"].lower(), apple_id, c["apple_music_interval"])
            row["animated"] = (("would be made smaller" + was) if too_big else "would be created") if url \
                else NO_ANIMATION[0]
        else:
            anim = animated_cover(cfg, apple_id)
            if anim:
                write_replacing(folder / "cover.webp", anim.webp)
                row["animated"] = (f"{'remade' if too_big else 'created'} ({anim.width}x{anim.height}, "
                                   f"{len(anim.webp) / 1e6:.1f} MB{', was ' + format(too_big, '.1f') + ' MB' if too_big else ''})")
            else:
                row["animated"] = NO_ANIMATION[0] + (f"; kept the existing {too_big:.1f} MB one" if too_big else "")
        if not dry_run and not (too_big and not row["animated"].startswith("remade")):
            mem["animated_cover"] = {"result": "created" if row["animated"].startswith(("created", "remade")) else row["animated"],
                                     "checked": albumstate.today()}

    # still naming
    notes = renamed + ([parked] if parked else [])
    if notes:
        row["still"] = ", ".join(notes) + (", " + row["still"] if row["still"] else "")
    if dry_run:
        moving = is_animated(folder / "cover.webp") or row["animated"] == "would be created"
        right, wrong = ((c["animated_still_name"], "cover.jpg") if moving else ("cover.jpg", c["animated_still_name"]))
        if cover:
            plan = f"would be saved as {right}"
        elif (folder / wrong).is_file() and not (folder / right).is_file():
            plan = f"{wrong} would be renamed to {right}"
        else:
            plan = "" if notes else "unchanged"
        row["still"] = ", ".join(x for x in (row["still"], plan) if x)
    elif cover:
        row["still"] += f", {settle_still(folder, cfg, cover.jpeg)}"
    else:
        done = settle_still(folder, cfg)
        if done != "none":
            row["still"] = (row["still"] + ", " if row["still"] else "") + done
    if not dry_run and json.dumps(mem, sort_keys=True) != before:
        albumstate.save(folder, mem)
    return row
