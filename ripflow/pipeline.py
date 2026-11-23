"""The shared pipeline: verify -> identify -> cover art -> tag -> file into the library.

Used directly by `ripflow process <folder>` and by the disc watcher after each rip.
"""
from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
import dataclasses
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import albumstate, changes, disclog
from .config import attention, log
from .animated import Unavailable as AnimatedUnavailable, animated_cover
from .cover import (apple_album_id, find_cover, is_animated, migrate_legacy_names, oversized_animation,
                    park_static_webp, settle_still, write_replacing)
from .dr import measure_album, write_log as write_dr_log
from .discid import Toc, toc_from_param, toc_from_samples
from .flacfile import FlacError, FlacInfo, read_info, read_picture, write_tags
from .musicbrainz import Album, MusicBrainz, identify
from .ripper import RipResult
from .util import AUDIO, NetworkError, NotFound, ascii_punctuation, similar, lead_int, natural_key, safe_component, safe_field
from .verify import check_many, pick_decoder



@dataclass
class Outcome:
    source: Path
    dest: Path | None = None
    label: str = ""
    problems: list[str] = field(default_factory=list)
    report: list[str] = field(default_factory=list)
    album_report: Path | None = None     # copy of the report kept in the album folder
    decision: str = ""                   # with --ask: "applied" (y), "skipped" (s) or "unchanged"
    release: str = ""                    # MusicBrainz release id used, if any

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclass
class Item:
    src: Path
    info: FlacInfo
    track: int
    title: str
    tags: dict[str, list[str]] | None    # None = leave the file's tags alone


def _kept(cfg: dict, info: FlacInfo) -> dict[str, list[str]]:
    """Existing tags allowed to survive a retag ([tags] keep; a trailing * matches a prefix)."""
    keep = [k.upper() for k in cfg["tags"]["keep"]]
    exact, prefixes = {k for k in keep if not k.endswith("*")}, tuple(k[:-1] for k in keep if k.endswith("*"))
    return {k: v for k, v in info.tags.items() if k in exact or (prefixes and k.startswith(prefixes))}


def disc_isrcs(own: list[str], musicbrainz: list[str], repeated: set[str]) -> tuple[list[str], str]:
    """A track's ISRCs and a note when one was replaced. The disc's own (or the file's) come
    first and alone; MusicBrainz's are used when the track has none. An ISRC that the disc gives
    to more than one track is likely a misread (drives can return the previous track's code), so
    it stays only where MusicBrainz agrees or knows none; otherwise MusicBrainz's replace it."""
    own = list(dict.fromkeys(v.upper().replace("-", "") for v in own if v.strip()))
    keep = [x for x in own if x not in repeated or x in musicbrainz or not musicbrainz]
    if own and not keep:
        return list(musicbrainz), (f"ISRC {', '.join(own)} is on another track of this disc too; "
                                   f"MusicBrainz has {', '.join(musicbrainz)} - using that")
    return keep or list(musicbrainz), ""


def _album_tags(cfg: dict, album: Album, n: int, info: FlacInfo, discid: str | None,
                repeated: set[str] = frozenset(), notes: list | None = None) -> dict[str, list[str]]:
    t = album.tracks[n - 1]
    one = lambda v: [str(v)] if v not in ("", None) else []
    tags = {
        "TITLE": one(t.title), "ARTIST": one(t.artist), "ARTISTS": t.artists, "ARTISTSORT": one(t.artist_sort),
        "ALBUM": one(album.title), "ALBUMARTIST": one(album.artist), "ALBUMARTISTS": album.artists,
        "ALBUMARTISTSORT": one(album.artist_sort),
        "TRACKNUMBER": one(n), "TRACKTOTAL": one(len(album.tracks)), "TOTALTRACKS": one(len(album.tracks)),
        "DISCNUMBER": one(album.disc), "DISCTOTAL": one(album.disc_total), "TOTALDISCS": one(album.disc_total),
        "DATE": one(album.date), "ORIGINALDATE": one(album.original_date),
        "ORIGINALYEAR": one(album.original_date[:4]), "GENRE": album.genres,
        "LABEL": album.labels, "CATALOGNUMBER": album.catalogs, "BARCODE": one(album.barcode),
        "ASIN": one(album.asin), "SCRIPT": one(album.script),
        "MEDIA": one(album.media), "RELEASECOUNTRY": one(album.country),
        "RELEASESTATUS": one(album.status), "RELEASETYPE": one(album.release_type),
        "COMPILATION": one(1) if album.various else [],
        "MUSICBRAINZ_ALBUMID": one(album.mbid), "MUSICBRAINZ_RELEASEGROUPID": one(album.group_id),
        "MUSICBRAINZ_ALBUMARTISTID": album.artist_ids, "MUSICBRAINZ_ARTISTID": t.artist_ids,
        "MUSICBRAINZ_TRACKID": one(t.recording_id), "MUSICBRAINZ_RELEASETRACKID": one(t.track_id),
    }
    kept = _kept(cfg, info)
    # ISRCs: the disc's / file's own; MusicBrainz's where there are none or one looks misread
    isrcs, note = disc_isrcs(kept.pop("ISRC", []), t.isrcs, repeated)
    if note and notes is not None:
        notes.append(f"track {n}: {note}")
    tags.update(kept)
    tags["ISRC"] = isrcs
    if discid:
        tags["MUSICBRAINZ_DISCID"] = [discid]
    return {k: v for k, v in tags.items() if v}


def _read_discid_txt(folder: Path) -> tuple[str | None, Toc | None]:
    f = folder / "discid.txt"
    if not f.is_file():
        return None, None
    kv = dict(l.split("=", 1) for l in f.read_text(encoding="utf-8", errors="replace").splitlines()
              if "=" in l and not l.startswith(("#", "http")))
    return kv.get("discid", "").strip() or None, toc_from_param(kv.get("toc", ""))


# Files ripflow makes itself. Brought along they would describe the old files, so they never
# are (rip logs of a ripped album are moved separately, see below).
REPORT = "ripflow-report.log"
GENERATED = ["discid.txt", albumstate.FILE, "dr.txt", "dr-disc*.txt", "rip.log", "rip-disc*.log", "*.ripflow-tmp",
             "ripflow-report*.log"]
GENERATED_COVERS = ["cover.jpg", "cover.webp", "cover orig.jpg", "cover orig.webp"]


PER_TRACK = {".lrc", ".elrc"}     # files that belong to one track and must keep its name
_NUMBERED = re.compile(r"(?:^|\s-\s)(?:(\d{1,2})-)?(\d{1,3})(?:\.|\s|-|_)+\s*(.*)$")


def match_track(stem: str, tracks: list[dict], disc: int = 1) -> dict | None:
    """The track a per-track file (lyrics) belongs to. tracks: dicts with src (source stem),
    new (new stem), track (number) and titles (source and new title).
    1. same name as the track's source file
    2. the track number in the name ("01. Title", "1-01 Title", "Artist - Album - 01 Title"),
       but only if the title in the name also resembles that track's title
    3. a near-identical title, when the name has no number"""
    for t in tracks:
        if t["src"].lower() == stem.lower():
            return t
    scored = [(fit, t) for t in tracks if (fit := match_fit(stem, t, tracks, disc)) is not None]
    return max(scored, key=lambda x: x[0])[1] if scored else None


def match_fit(stem: str, t: dict, tracks: list[dict], disc: int = 1) -> float | None:
    """How well a per-track file name fits track t (higher is better), or None if it doesn't:
    rule 2 or 3 of match_track. The fit is how closely the title in the name resembles the
    track's, so of two lyric files for one track ("01. Title" and "01. Title - old copy") the
    one whose title is closer wins."""
    title = lambda text: max((similar(text, x) for x in t["titles"] if x), default=0)
    m = _NUMBERED.search(stem)
    if m:
        d, n, text = int(m.group(1) or disc), int(m.group(2)), m.group(3).strip()
        if d != disc or t["track"] != n or sum(x["track"] == n for x in tracks) != 1:
            return None
        if not text:
            return 0.6
        fit = title(text)
        return fit if fit >= 0.6 else None
    fit = title(stem)
    return fit if fit >= 0.9 else None


def plan_extras(cfg: dict, src_dir: Path, dest_dir: Path, tracks: list[dict], disc: int = 1,
                covers_managed: bool = True) -> list[tuple[Path, Path]]:
    """(source, destination) for the extra files to bring along. A file named exactly like a
    source track follows that track's new name; a lyric file is matched to its track even if
    the names differ (see match_track). Cover files are left behind only when ripflow makes
    the covers this run (not with --no-cover)."""
    patterns = [p.lower() for p in cfg["files"]["extras"]]
    still = cfg["cover"]["animated_still_name"]
    skip = [g.lower() for g in GENERATED + ((GENERATED_COVERS + [still, Path(still).stem + ".webp"])
                                            if covers_managed else [])]
    files = [f for f in sorted(src_dir.iterdir(), key=lambda p: natural_key(p.name))
             if f.is_file() and not any(fnmatch.fnmatchcase(f.name.lower(), g) for g in skip)
             and any(fnmatch.fnmatchcase(f.name.lower(), p) for p in patterns)]
    exact = lambda f: next((t for t in tracks if t["src"].lower() == f.stem.lower()), None)
    chosen = {f: exact(f) for f in files}
    # Per file, not per album: a lyric file still named like its track keeps that match, and
    # only the others (e.g. left with an old name when the tracks were converted and renamed)
    # are matched by number and title - among the tracks no lyric file matched exactly.
    # The closest fits are settled first, so a stray copy can't take a track from its own
    # lyric file just because its name sorts first.
    claimed = {id(t) for f, t in chosen.items() if t and f.suffix.lower() in PER_TRACK}
    pairs = sorted(((fit, i, f, t) for i, f in enumerate(files)
                    if chosen[f] is None and f.suffix.lower() in PER_TRACK
                    for t in tracks if id(t) not in claimed
                    and (fit := match_fit(f.stem, t, tracks, disc)) is not None),
                   key=lambda x: (-x[0], x[1]))
    for fit, i, f, t in pairs:
        if chosen[f] is None and id(t) not in claimed:
            chosen[f] = t
            claimed.add(id(t))
    plan = []
    for f in files:
        t = chosen[f]
        target = dest_dir / ((t["new"] + f.suffix) if t else f.name)
        if not _same(f, target):
            plan.append((f, target))
    return plan


def _same(a: Path, b: Path) -> bool:
    try:
        return a.exists() and b.exists() and os.path.samefile(a, b)
    except OSError:
        return False


def process_folder(cfg: dict, folder: Path, *, move: bool = False, dry_run: bool = False,
                   forced_release: str | None = None, rip: RipResult | None = None,
                   no_cover: bool = False, ignore_local_art: bool = False, only_local_art: bool = False,
                   chooser=None, confirm: bool = False, show_art: bool = False,
                   prefer_discid: bool = False) -> list[Outcome]:
    """Process every album found under `folder`. One album = one directory (+ disc number).

    confirm (process --ask, review): before changing anything, show a before/after table and
    ask y (go ahead), n (wrong release: pick another) or s (skip the album; Outcome.decision
    says which). show_art: also open a before/after picture of the cover art."""
    folder = Path(folder)
    if cfg["paths"]["library"] is None:
        raise SystemExit("set [paths] library in ripflow.toml first")
    files = sorted((p for p in folder.rglob("*") if p.suffix.lower() == ".flac" and p.is_file()),
                   key=lambda p: (str(p.parent), natural_key(p.name)))
    infos: dict[Path, FlacInfo | None] = {}
    for p in files:
        try:
            infos[p] = read_info(p)
        except (FlacError, OSError):
            infos[p] = None
    groups: dict[tuple, list[Path]] = {}
    for p in files:
        broken = rip is not None or any(infos[q] is None for q in files if q.parent == p.parent)
        disc = 0 if broken else lead_int(infos[p].tag("DISCNUMBER"), 1)
        groups.setdefault((p.parent if rip is None else folder, disc), []).append(p)
    if not groups:
        log.warning("no FLAC files under %s", folder)
    # An album folder with other audio files besides FLAC (say some tracks are .opus) is left
    # alone: ripflow would see too few tracks to match it, and would split the album.
    outcomes: list[Outcome] = []
    if rip is None:
        for key in list(groups):
            others = sorted((p.name for p in key[0].iterdir()
                             if p.is_file() and p.suffix.lower() in AUDIO - {".flac"}), key=natural_key)
            if others:
                del groups[key]
                if any(o.source == key[0] for o in outcomes):
                    continue                 # one note per folder (multi-disc groups)
                out = Outcome(source=key[0], label=key[0].name)
                kinds = ", ".join(sorted({Path(n).suffix.lower() for n in others}))
                out.problems.append(f"has {len(others)} non-FLAC audio file(s) ({kinds}) - left untouched; "
                                    "replace them with FLAC to process this album")
                out.report += [f"Folder: {key[0]}", "Non-FLAC audio files:"] + [f"  {n}" for n in others]
                outcomes.append(_finish(cfg, out, dry_run))
    mb = MusicBrainz(cfg)
    per_folder = {d: sum(1 for k in groups if k[0] == d) for d, _ in groups}
    return outcomes + [_process_album(cfg, mb, g, infos, move=move, dry_run=dry_run, forced_release=forced_release,
                           rip=rip, no_cover=no_cover, ignore_local_art=ignore_local_art,
                           only_local_art=only_local_art, chooser=chooser, confirm=confirm,
                           show_art=show_art, prefer_discid=prefer_discid,
                           ndiscs=None if rip else per_folder[key[0]])
            for key, g in groups.items()]


def _process_album(cfg, mb, files, infos, *, move, dry_run, forced_release, rip, no_cover,
                   ignore_local_art=False, only_local_art=False, chooser=None, confirm=False,
                   show_art=False, repick=False, checks=None, turned_down="", prefer_discid=False,
                   ndiscs=None) -> Outcome:
    src_dir = files[0].parent
    out = Outcome(source=src_dir)
    say = out.report.append
    stamp = datetime.now().strftime("%Y-%m-%d %H.%M.%S")
    say(f"ripflow report  {stamp}")
    say(f"source: {src_dir}  ({len(files)} files, {'ripped from CD' if rip else 'existing files'})")

    # 1. integrity -------------------------------------------------------------
    checks = checks or check_many(files, pick_decoder(cfg), cfg["verify"]["jobs"])
    say("\n== File integrity (full decode vs header, audio MD5) ==")
    for c in checks:
        say(f"  {c.status:18} md5={c.md5:8} {c.path.name}" + (f"  [{c.errors}]" if c.errors else "")
            + (f"  missing {c.missing_seconds:.3f}s" if c.status == "TRUNCATED" else ""))
        if not c.ok:
            out.problems.append(f"integrity {c.status}: {c.path.name}")
    if any(infos[p] is None for p in files):
        out.problems.append("unreadable FLAC file(s) - album left untouched")
        return _finish(cfg, out, dry_run)

    # 2. rip verification ------------------------------------------------------
    if rip:
        say(f"\n== AccurateRip (from the cyanrip log): {rip.status} ==")
        for t in rip.tracks:
            say(f"  track {t.number:2}: {t.state:10} conf={t.confidence:<4} {t.detail}")
        if rip.message:
            say(f"  note: {rip.message}")
        why = {"UNVERIFIED": "disc is not in AccurateRip, so the rip could not be cross-checked",
               "SUSPECT": "AccurateRip MISMATCH on track(s) "
                          + ", ".join(str(t.number) for t in rip.tracks if t.state == "mismatch"),
               "FAILED": "rip reported failures: " + rip.message,
               "UNKNOWN": "could not read AccurateRip results from the rip log " + rip.message}
        if rip.status in why:
            out.problems.append(why[rip.status])

    # 3. order, disc id, identification ---------------------------------------
    numbers = [lead_int(infos[p].tag("TRACKNUMBER")) for p in files]
    if all(numbers) and len(set(numbers)) == len(numbers):
        files = [p for _, p in sorted(zip(numbers, files), key=lambda x: x[0])]
    flacs = [infos[p] for p in files]
    if rip and len(rip.tracks) == len(flacs):       # ISRCs from the rip log, if the files lack them
        for info, t in zip(flacs, rip.tracks):
            if t.isrc and not info.tag("ISRC"):
                info.tags["ISRC"] = [t.isrc]

    txt_id, txt_toc = _read_discid_txt(src_dir)
    tag_ids = {i.tag("MUSICBRAINZ_DISCID", "DISCID") for i in flacs}
    # the disc as ripflow.json recorded it (at rip time, or `ripflow discids` from a rip log)
    known = None if rip else disclog.stored(albumstate.load(src_dir), lead_int(flacs[0].tag("DISCNUMBER"), 1))
    learned_disc = None
    if not rip and not known:
        # no record yet: a rip log of this disc in the folder (cyanrip's) serves as well
        samples = [i.total_samples for i in flacs]
        log_disc = next((d for d in disclog.logs_in(src_dir)
                         if d.source.lower().startswith("rip") and disclog.fits(d.toc, samples)), None)
        if log_disc:
            known = log_disc.entry(albumstate.today())
            learned_disc = (lead_int(flacs[0].tag("DISCNUMBER"), 1), known)
    known_toc = toc_from_param(known.get("toc", "")) if known else None
    if known and (not known_toc or known_toc.discid != known["discid"]
                  or not disclog.fits(known_toc, [i.total_samples for i in flacs])):
        say(f"  ripflow.json's disc {known['discid']} does not match these files - not used")
        known = known_toc = None
    discid = (rip.discid if rip else None) or (known and known["discid"]) or txt_id \
        or (tag_ids.pop() if len(tag_ids) == 1 else "") or None
    cd_like = all(i.sample_rate == 44100 and i.bps == 16 and i.channels == 2 for i in flacs)
    toc = known_toc or txt_toc or (toc_from_samples([i.total_samples for i in flacs]) if cd_like else None)
    how = f" (from ripflow.json, recorded from {known.get('from', '?')})" if known else ""
    say(f"\n== Identification ==\n  disc id: "
        f"{(discid + how) if discid else (toc.discid + ' (computed from track lengths)' if toc else 'n/a')}")
    if toc:
        say(f"  toc: {toc.toc_param}")

    # A folder that still carries our discid.txt / rip log is one of our own CD rips.
    from_cd = bool(rip or known or txt_id or any(src_dir.glob("rip*.log")))
    say(f"  treated as: {'CD rip (prefer CD releases)' if from_cd else 'existing files (prefer Digital Media)'}")
    if turned_down:
        say(f"  you turned down release {turned_down}; picking again")
    if known and known.get("mcn"):
        # the barcode the disc itself carries decides between releases (not written as a tag)
        id_infos = [dataclasses.replace(i, tags={**i.tags, "DISC_MCN": [known["mcn"]]}) for i in flacs]
    else:
        id_infos = flacs
    ident = identify(mb, cfg, id_infos, discid=discid, toc=toc, forced_release=None if repick else forced_release,
                     source="rip" if from_cd else "files", chooser=chooser, repick=repick,
                     prefer_discid=prefer_discid, ndiscs=ndiscs)
    say(f"  method: {ident.method}")
    for line in ident.candidates + [f"  note: {n}" for n in ident.notes]:
        say("  " + line)
    if ident.skipped:
        out.label = src_dir.name
        out.decision = "skipped"
        say("\n(skipped at your request - nothing was changed)")
        return _finish(cfg, out, dry_run)

    if confirm and not dry_run and ident.offline:
        out.label = src_dir.name
        out.problems.append("MusicBrainz unreachable - album left untouched, re-run later")
        return _finish(cfg, out, dry_run)

    naming = cfg["naming"]
    album = ident.album
    have_tags = not rip and all(i.tag("ALBUM") and i.tag("TITLE") for i in flacs)
    unknown = False
    if album:
        artist, title, year = album.artist, album.title, (album.original_date or album.date)[:4]
        disc, disc_total, various = album.disc, album.disc_total, album.various
        seen = [v.upper().replace("-", "") for i in flacs for v in dict.fromkeys(i.tags.get("ISRC", []))]
        repeated = {v for v in seen if seen.count(v) > 1}
        isrc_notes: list[str] = []
        items = [Item(p, i, n, album.tracks[n - 1].title,
                      _album_tags(cfg, album, n, i, discid, repeated, isrc_notes))
                 for n, (p, i) in enumerate(zip(files, flacs), 1)]
        for line in isrc_notes:
            say(f"  {line}")
        out.label = f"{artist} - {title}"
        out.release = album.mbid
    elif have_tags:
        f0 = flacs[0]
        artist, title = f0.tag("ALBUMARTIST", "ALBUM ARTIST", "ARTIST", default=naming["unknown_artist"]), f0.tag("ALBUM")
        year, disc, disc_total, various = f0.tag("DATE")[:4], lead_int(f0.tag("DISCNUMBER"), 1), \
            lead_int(f0.tag("DISCTOTAL", "TOTALDISCS"), 1), False
        items = [Item(p, i, lead_int(i.tag("TRACKNUMBER"), n), i.tag("TITLE"), None)
                 for n, (p, i) in enumerate(zip(files, flacs), 1)]
        out.label = f"{artist} - {title}"
        out.problems.append("no MusicBrainz match - kept the existing tags"
                            + (" (MusicBrainz unreachable, re-run later)" if ident.offline else ""))
    else:
        unknown = True
        artist = naming["unknown_artist"]
        title = rip.folder.name if rip else stamp
        year, disc, disc_total, various = "", 1, 1, False
        items = []
        for n, (p, i) in enumerate(zip(files, flacs), 1):
            tags = {"TITLE": [f"Track {n:02d}"], "ARTIST": [artist], "ALBUMARTIST": [artist],
                    "ALBUM": [title], "TRACKNUMBER": [str(n)], "TRACKTOTAL": [str(len(files))],
                    **_kept(cfg, i), **({"MUSICBRAINZ_DISCID": [discid]} if discid else {})}
            items.append(Item(p, i, n, f"Track {n:02d}", {k: v for k, v in tags.items() if v}))
        out.label = f"{artist} - {title}"
        out.problems.append("no MusicBrainz match - filed as Unknown"
                            + (" (MusicBrainz unreachable, re-run `ripflow process` on the folder later)"
                               if ident.offline else " (add the disc on MusicBrainz, then re-run "
                                                     "`ripflow process` on the folder)"))

    plain = ascii_punctuation if cfg["tags"]["ascii_punctuation"] else (lambda s: s)
    fields = {"albumartist": safe_field(plain(artist)), "album": safe_field(plain(title)),
              "year": year or "0000", "disc": disc}
    # ripped CDs go to [paths] ripped (by default the library), everything else to the library
    root = cfg["paths"]["ripped"] if rip else cfg["paths"]["library"]
    dest_dir = Path(root).joinpath(
        *[safe_component(part.format(**fields), is_dir=True) for part in naming["album_dir"].split("/")])
    pattern = naming["track_multidisc"] if disc_total > 1 else naming["track"]
    targets = [dest_dir / (safe_component(pattern.format(
                   track=it.track, title=safe_field(plain(it.title)),
                   artist=safe_field(plain(it.info.tag("ARTIST") if it.tags is None
                                           else it.tags.get("ARTIST", [artist])[0])),
                   **fields)) + ".flac") for it in items]
    out.dest = dest_dir
    say(f"\n== Destination ==\n  {dest_dir}")
    for it, tgt in zip(items, targets):
        say(f"  {it.src.name}  ->  {tgt.name}")
    covers_managed = cfg["cover"]["enabled"] and not no_cover
    track_list = [dict(src=it.src.stem, new=t.stem, track=it.track, titles=[it.info.tag("TITLE"), it.title])
                  for it, t in zip(items, targets)]
    extras = [] if rip else plan_extras(cfg, src_dir, dest_dir, track_list, disc, covers_managed)
    for f, t in extras:
        say(f"  extra: {f.name}" + (f"  ->  {t.name}" if t.name != f.name else ""))

    conflicts = [t for it, t in zip(items, targets) if t.exists() and not _same(it.src, t)]
    if len(set(targets)) != len(targets):
        conflicts.append(dest_dir / "(two tracks map to the same file name)")
    if conflicts:
        out.problems.append(f"destination already has {len(conflicts)} of these files "
                            f"(e.g. {conflicts[0].name}) - nothing moved, source left in {src_dir}")
        out.dest = None
        return _finish(cfg, out, dry_run)
    if dry_run:
        say("\n(dry run - nothing was changed)")
        return _finish(cfg, out, dry_run)

    # 4. cover art -------------------------------------------------------------
    cover = animated = None
    # identify the album at the stores by barcode first, then by name, track count and edition
    product = dict(barcode=album.barcode if album else flacs[0].tag("BARCODE", "UPC"),
                   edition_note=album.disambiguation if album else "")
    # album memory (ripflow.json): the destination's own, or the source's when it has none yet
    memory = albumstate.load(dest_dir) or albumstate.load(src_dir)
    learned: dict = {}
    ask_now = confirm and not dry_run
    make_animated = None
    renamed: list[str] = []
    parked: str | None = None
    keep_anim: Path | None = None          # an existing animated cover.webp to keep
    anim_plan = "not looked for"
    if cfg["cover"]["enabled"] and not no_cover and (not unknown or only_local_art):
        found: dict = {}
        # with --ask these renames wait for your answer; now they are only described
        renamed = migrate_legacy_names(dest_dir, cfg, dry_run=ask_now) if dest_dir.is_dir() else []
        parked = park_static_webp(dest_dir, cfg, dry_run=ask_now) if dest_dir.is_dir() else None
        cover, notes = find_cover(cfg, artist=artist, album=title, various=various, ntracks=len(items),
                                  mbid=album.mbid if album else "", group_id=album.group_id if album else "",
                                  local_dirs=[] if ignore_local_art else [dest_dir, src_dir],
                                  embedded_from=None if ignore_local_art else files[0],
                                  only_local=only_local_art, found=found, **product)
        say("\n== Cover art ==")
        for line in renamed + ([parked] if parked else []):
            say(f"  {line}")
        for n in notes:
            say("  " + n)
        if cover is None:
            out.problems.append(f"no cover art of at least {cfg['cover']['min_size']}px found")
        else:
            say(f"  chosen: {cover.source} {cover.width}x{cover.height}")
        # Apple Music's animated cover -> cover.webp. An existing animated one (in the library
        # folder, or brought from the source folder) is kept unless --ignore-local-art, or it
        # is over the size limit and Apple Music has a new one.
        if not ignore_local_art:
            keep_anim = next((c for c in dict.fromkeys([dest_dir / "cover.webp", src_dir / "cover.webp"])
                              if is_animated(c)), None)
        too_big = oversized_animation(keep_anim, cfg) if keep_anim else 0.0
        wants_anim = cfg["cover"]["animated"] and not only_local_art and not unknown
        seen = memory.get("animated_cover") or {}
        if wants_anim and not keep_anim and not ignore_local_art and seen.get("result") in albumstate.NO_ANIMATION:
            # Apple Music had none when last asked (`covers --recheck` or --ignore-local-art asks again)
            wants_anim = False
            anim_plan = f"none ({seen['result']}, checked {seen.get('checked', '?')})"
        if wants_anim:
            anim_plan = (f"made again from Apple Music (the existing one is {too_big:.1f} MB)" if too_big else
                         "keep the existing cover.webp" if keep_anim else
                         "looked for on Apple Music" + (" after you confirm" if ask_now else ""))
        elif keep_anim:
            anim_plan = "keep the existing cover.webp"

        def make_animated():
            if not wants_anim:
                return None
            if keep_anim and not too_big:
                say("  animated cover: keeping the existing cover.webp")
                return None
            if too_big:
                say(f"  animated cover: the existing cover.webp is {too_big:.1f} MB, over the "
                    f"{cfg['cover']['animated_max_mb']:g} MB limit - making it again")
            storefront = cfg["cover"]["itunes_country"].lower()
            stored = memory.get("apple") or {}
            known = stored.get("album_id") if stored.get("storefront") == storefront and not ignore_local_art else None
            apple_id = found.get("apple_album_id") or known or apple_album_id(
                cfg, artist=artist, album=title, various=various, ntracks=len(items), **product)
            made, result = None, None
            if not apple_id:
                result = "album not found on Apple Music"
                say(f"  animated cover: {result}")
            else:
                learned["apple"] = {"album_id": apple_id, "storefront": storefront}
                try:
                    made = animated_cover(cfg, apple_id)
                    result = "created" if made else "no animated cover on Apple Music"
                    say(f"  {made.note}" if made else f"  animated cover: {result}"
                        + (" (keeping the existing cover.webp)" if too_big else ""))
                except (AnimatedUnavailable, NetworkError, NotFound, OSError, subprocess.SubprocessError) as e:
                    say(f"  animated cover: not available ({e})")
            if result:
                learned["animated_cover"] = {"result": result, "checked": albumstate.today()}
            return made

        if not ask_now:
            animated = make_animated()

    # --ask: show everything that is about to change and wait for your answer ---------
    if ask_now:
        rows = _change_rows(cfg, src_dir, dest_dir, files, items, targets, extras, move, cover,
                            renamed + ([parked] if parked else []), anim_plan, no_cover)
        print(f"\n== Proposed changes: {out.label} ==")
        if album:
            facts = [x for x in (album.country, album.date, album.media, ", ".join(album.labels[:2]),
                                 album.barcode and f"barcode {album.barcode}") if x]
            print(f"MusicBrainz: {album.title} ({' | '.join(facts)})  found by: {ident.method}\n"
                  f"             https://musicbrainz.org/release/{album.mbid}")
        else:
            print("MusicBrainz: no match" + (" (unreachable)" if ident.offline else ""))
        for p in out.problems:
            print(f"Attention: {p}")
        same_embed = cover is not None and all(_embedded(f) == cover.embed[0] for f in files)
        if _nothing_to_change(rows, same_embed, cover):
            out.decision = "unchanged"
            out.dest = src_dir                 # nothing was filed anywhere
            log.info("No changes needed: %s", out.label)
            say("\n  No changes needed - nothing was written")
            return _finish(cfg, out, dry_run)
        print(changes.render(rows) if rows else "(no changes to tags, names or art)")
        art_shown = cfg["cover"]["enabled"] and not no_cover
        view = (lambda: _show_art(cfg, src_dir, dest_dir, files[0], cover, no_cover)) if art_shown else None
        if show_art and view:
            view()
        answer = changes.ask(view)
        if answer == "n":
            if chooser is None:
                print("  (no release prompt available here, so the album is skipped)")
                answer = "s"
            else:
                return _process_album(cfg, mb, files, infos, move=move, dry_run=dry_run, forced_release=None,
                                      rip=rip, no_cover=no_cover, ignore_local_art=ignore_local_art,
                                      only_local_art=only_local_art, chooser=chooser, confirm=confirm,
                                      show_art=show_art, repick=True, checks=checks, prefer_discid=prefer_discid,
                                      ndiscs=ndiscs,
                                      turned_down=album.mbid if album else "(no match)")
        if answer == "s":
            out.decision = "skipped"
            say("\n(skipped at your request - nothing was changed)")
            return _finish(cfg, out, dry_run)
        out.decision = "applied"
        say("\n  you confirmed the changes")
        if cfg["cover"]["enabled"] and not no_cover and (not unknown or only_local_art) and dest_dir.is_dir():
            for line in migrate_legacy_names(dest_dir, cfg) + [x for x in [park_static_webp(dest_dir, cfg)] if x]:
                say(f"  {line}")
        if make_animated:
            animated = make_animated()

    # 5. dynamic range (measured now, written with the other tags) --------------
    dr_rows = None
    if cfg["dr"]["enabled"]:
        try:
            album_dr, track_drs = measure_album(flacs, pick_decoder(cfg), cfg["verify"]["jobs"])
            say(f"\n== Dynamic range ==\n  album DR{album_dr}: " + " ".join(f"DR{t.dr}" for t in track_drs))
            dr_rows = list(zip([t.name for t in targets], track_drs))
            if cfg["dr"]["write_tags"]:
                for it, t in zip(items, track_drs):
                    it.tags = dict(it.tags if it.tags is not None else it.info.tags)
                    it.tags["DYNAMIC RANGE"] = [str(t.dr)]
                    it.tags["ALBUM DYNAMIC RANGE"] = [str(album_dr)]
        except ImportError:
            out.problems.append("dynamic range skipped: numpy is not installed (pip install numpy)")
        except Exception as e:
            out.problems.append(f"dynamic range measurement failed: {e!r}")

    # 6. file + tag ------------------------------------------------------------
    dest_dir.mkdir(parents=True, exist_ok=True)
    # Within the album's own folder a new name is a rename; copying would leave the old file beside it.
    relocate = shutil.move if move or _same(src_dir, dest_dir) else shutil.copy2
    for f, t in extras:
        if t.exists():
            say(f"  extra not copied, {t.name} already exists in the destination")
            continue
        relocate(str(f), str(t))
    for it, tgt in zip(items, targets):
        if not _same(it.src, tgt):
            relocate(str(it.src), str(tgt))
        if it.tags is not None or cover:
            write_tags(tgt, it.tags if it.tags is not None else it.info.tags,
                       picture=cover.embed if cover else None)
            after = read_info(tgt)
            if (after.md5, after.total_samples) != (it.info.md5, it.info.total_samples):
                out.problems.append(f"header changed unexpectedly after tagging: {tgt.name}")
    if animated:
        write_replacing(dest_dir / "cover.webp", animated.webp)
    elif keep_anim and keep_anim.parent != dest_dir and not (dest_dir / "cover.webp").exists():
        # (an oversized one too, when no new one could be made: better than none)
        relocate(str(keep_anim), str(dest_dir / "cover.webp"))
        say("  animated cover.webp brought along from the source folder")
    if cover:
        say(f"  still cover {settle_still(dest_dir, cfg, cover.jpeg)}")
    if dr_rows and cfg["dr"]["write_log"]:
        write_dr_log(dest_dir / ("dr.txt" if disc_total == 1 else f"dr-disc{disc}.txt"),
                     artist, title, album_dr, dr_rows)

    # 7. ReplayGain (rsgain scans the whole album folder and writes its own tags) ---
    if cfg["rsgain"]["enabled"]:
        exe = shutil.which(cfg["tools"]["rsgain"])
        if not exe:
            out.problems.append("rsgain not found - ReplayGain tags not written (set [tools] rsgain)")
        else:
            cmd = [exe, *[str(a).replace("{album}", str(dest_dir)) for a in cfg["rsgain"]["args"]]]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=3600,
                                   stdin=subprocess.DEVNULL, cwd=dest_dir)
                gained = sum(1 for t in targets if read_info(t).tag("REPLAYGAIN_TRACK_GAIN"))
                say(f"\n== ReplayGain ==\n  rsgain exit code {r.returncode}; {gained}/{len(targets)} tracks tagged")
                if r.returncode != 0 or gained != len(targets):
                    out.problems.append(f"rsgain did not tag every track (exit {r.returncode}): "
                                        + (r.stderr or r.stdout).strip()[-300:])
            except (OSError, subprocess.SubprocessError, FlacError) as e:
                out.problems.append(f"rsgain failed: {e!r}")
    if rip and rip.log_path and rip.log_path.is_file():
        shutil.copy2(rip.log_path, dest_dir / ("rip.log" if disc_total == 1 else f"rip-disc{disc}.log"))
    if unknown and (discid or toc):
        lines = [f"discid={discid or toc.discid}"]
        if toc:
            lines += [f"toc={toc.toc_param}", "# To add this disc to MusicBrainz, open:", toc.attach_url]
        (dest_dir / "discid.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    elif (src_dir / "discid.txt").is_file() and move and not _same(src_dir, dest_dir):
        (src_dir / "discid.txt").unlink()
    # a ripped disc: its disc ID, exact TOC and the disc's own barcode, for later `process` runs
    if rip and rip.discid and rip.log_path and rip.log_path.is_file():
        rtoc, logged, mcn = disclog.from_cyanrip(disclog.read_text(rip.log_path))
        if rtoc and rtoc.discid == rip.discid:
            entry = disclog.DiscLog(rtoc, "rip.log" if disc_total == 1 else f"rip-disc{disc}.log",
                                    disc, mcn).entry(albumstate.today())
        else:
            entry = {"discid": rip.discid, "from": "rip", "recorded": albumstate.today()}
        learned[disclog.KEY] = {**(memory.get(disclog.KEY) or {}), str(disc or 1): entry}
    elif learned_disc and not disclog.stored(memory, learned_disc[0]):
        # read from the folder's rip log this time: record it for next time
        learned[disclog.KEY] = {**(memory.get(disclog.KEY) or {}), str(learned_disc[0]): learned_disc[1]}
    # album memory: whatever the source folder knew, plus what was learned now
    if memory or learned:
        albumstate.save(dest_dir, {**memory, **learned})
        if move and not _same(src_dir, dest_dir) and (src_dir / albumstate.FILE).is_file():
            (src_dir / albumstate.FILE).unlink()
    if not rip and not _same(src_dir, dest_dir):
        # the rip logs of an earlier `watch`/`rip` (rip.log, rip-disc2.log...) come along, copied
        # or moved like the tracks; they describe how the audio was read, which stays true
        for extra in sorted(src_dir.iterdir(), key=lambda f: natural_key(f.name)):
            name = extra.name.lower()
            if extra.is_file() and (name == "rip.log" or fnmatch.fnmatchcase(name, "rip-disc*.log")) \
                    and not (dest_dir / extra.name).exists():
                relocate(str(extra), str(dest_dir / extra.name))
                say(f"  rip log brought along: {extra.name}")
    if move and not _same(src_dir, dest_dir):
        for old in src_dir.glob("ripflow-report*.log"):  # it describes the files as they were
            old.unlink()
    if move and not _same(src_dir, dest_dir) and src_dir.is_dir() and not any(src_dir.iterdir()):
        src_dir.rmdir()
    out.album_report = dest_dir / (REPORT if disc_total == 1 else f"ripflow-report-disc{disc}.log")
    return _finish(cfg, out, dry_run)


def _current_still(cfg: dict, *folders: Path) -> Path | None:
    """The still cover image already in the album folder, if any."""
    still = cfg["cover"]["animated_still_name"]
    names = ["cover.jpg", still, "cover orig.jpg", "cover.png", Path(still).stem + ".webp", "folder.jpg", "front.jpg"]
    return next((f / n for f in dict.fromkeys(folders) for n in names if (f / n).is_file()), None)


def _embedded(path: Path) -> bytes | None:
    try:
        return read_picture(path)
    except (FlacError, OSError):
        return None


def _change_rows(cfg, src_dir, dest_dir, files, items, targets, extras, move, cover, cover_renames,
                 anim_plan, no_cover) -> list[tuple[str, str, str]]:
    """What --ask shows: (what, before, after) for everything this run is about to change."""
    rows: list[tuple[str, str, str]] = []
    if _same(src_dir, dest_dir):
        verb = "renamed in place"
    else:
        verb = "moved to " if move else "copied to "
        lib = Path(cfg["paths"]["library"])
        where = f"<library>{os.sep}{dest_dir.relative_to(lib)}" if lib in dest_dir.parents else str(dest_dir)
        rows.append(("folder", str(src_dir), verb + where))
    for n, (it, tgt) in enumerate(zip(items, targets), 1):
        if it.src.name != tgt.name:
            rows.append((f"file #{n}", it.src.name, tgt.name))
    for f, t in extras:
        rows.append(("extra file", f.name, t.name if t.name != f.name else "brought along"))
    if not _same(src_dir, dest_dir):
        for f in sorted(src_dir.glob("*.log"), key=lambda f: natural_key(f.name)):
            if f.name.lower() == "rip.log" or fnmatch.fnmatchcase(f.name.lower(), "rip-disc*.log"):
                rows.append(("rip log", f.name, "brought along"))
    rows += changes.tag_rows([it.info.tags for it in items], [it.tags for it in items])
    if cfg["cover"]["enabled"] and not no_cover:
        current = _current_still(cfg, src_dir, dest_dir)
        old = current.read_bytes() if current else None
        before = f"{current.name} {changes.picture_size(old)}" if current else changes.NONE
        if cover is None:
            rows.append(("cover image", before, "unchanged (no new cover found)"))
        else:
            same = " (the same image)" if old == cover.jpeg else ""
            rows.append(("cover image", before, f"{cover.source} {cover.width}x{cover.height}{same}"))
            ew, eh = cover.embed[1], cover.embed[2]
            rows.append(("embedded art", changes.picture_size(_embedded(files[0])),
                         f"{ew}x{eh} from the cover image, in every track"))
        for line in cover_renames:
            rows.append(("cover files", "", line.replace("would be ", "")))
        rows.append(("animated cover", "cover.webp" if (src_dir / "cover.webp").exists()
                     or (dest_dir / "cover.webp").exists() else changes.NONE, anim_plan))
    if cfg["dr"]["enabled"]:
        old_dr = items[0].info.tag("ALBUM DYNAMIC RANGE") if items else ""
        rows.append(("dynamic range", f"DR{old_dr}" if old_dr else changes.NONE, "measured after you confirm"))
    if cfg["rsgain"]["enabled"]:
        old_rg = items[0].info.tag("REPLAYGAIN_ALBUM_GAIN") if items else ""
        rows.append(("ReplayGain", old_rg or changes.NONE, "rsgain runs after you confirm"))
    return rows


def _nothing_to_change(rows, same_embed: bool, cover) -> bool:
    """True when the --ask rows hold nothing that would change the album (copying it as it is
    to another folder does not count)."""
    def unchanged(what: str, before: str, after: str) -> bool:
        if what in ("folder", "rip log"):
            return True
        if what == "extra file":
            return after == "brought along"
        if what == "cover image":
            return after.startswith("unchanged") or after.endswith("(the same image)")
        if what == "embedded art":
            return cover is None or same_embed
        if what == "animated cover":
            return not after.startswith(("looked for", "made again"))
        if what in ("dynamic range", "ReplayGain"):
            return before != changes.NONE
        return False                       # renamed files, tags, cover file renames
    return all(unchanged(*r) for r in rows)


def _show_art(cfg, src_dir, dest_dir, first_file, cover, no_cover) -> None:
    """Open a before/after picture of the cover art in the default image viewer."""
    if no_cover or not cfg["cover"]["enabled"]:
        return
    current = _current_still(cfg, src_dir, dest_dir)
    tiles = [(f"now: {current.name}" if current else "now: no cover file", current.read_bytes() if current else None),
             ("now: embedded", _embedded(first_file)),
             (f"new: {cover.source}" if cover else "new: none found", cover.jpeg if cover else None)]
    preview = changes.art_preview(tiles)
    if preview:
        print(f"  cover art before/after: {preview}")
        changes.open_file(preview)
    else:
        print("  (the art preview needs Pillow)")


def _finish(cfg: dict, out: Outcome, dry_run: bool) -> Outcome:
    out.report.append("\n== Result ==")
    out.report += [f"  ATTENTION: {p}" for p in out.problems] or ["  all good"]
    text = "\n".join(out.report) + "\n"
    if dry_run:
        print(text)
        return out
    reports = Path(cfg["paths"]["logs"]) / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    name = safe_component(f"{datetime.now():%Y-%m-%d %H.%M.%S} {out.label or out.source.name}", limit=150) + ".txt"
    (reports / name).write_text(text, encoding="utf-8")
    if out.album_report and out.album_report.parent.is_dir():
        try:                                   # the latest run's report, beside the files it describes
            out.album_report.write_text(text, encoding="utf-8")
        except OSError as e:
            log.warning("could not write %s: %s", out.album_report, e)
    where = out.dest or out.source
    log.info("%s -> %s (%s)", out.label or out.source.name, where,
             "OK" if out.ok else f"{len(out.problems)} issue(s)")
    for p in out.problems:
        attention(f"{where} :: {p}")
    return out
