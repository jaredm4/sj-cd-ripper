"""The shared pipeline: verify -> identify -> cover art -> tag -> file into the library.

Used directly by `ripflow process <folder>` and by the disc watcher after each rip.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import attention, log
from .cover import find_cover
from .dr import measure_album, write_log as write_dr_log
from .discid import Toc, toc_from_param, toc_from_samples
from .flacfile import FlacError, FlacInfo, read_info, write_tags
from .musicbrainz import Album, MusicBrainz, identify
from .ripper import RipResult
from .util import lead_int, natural_key, safe_name
from .verify import check_many, pick_decoder



@dataclass
class Outcome:
    source: Path
    dest: Path | None = None
    label: str = ""
    problems: list[str] = field(default_factory=list)
    report: list[str] = field(default_factory=list)

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


def _album_tags(cfg: dict, album: Album, n: int, info: FlacInfo, discid: str | None) -> dict[str, list[str]]:
    t = album.tracks[n - 1]
    one = lambda v: [str(v)] if v not in ("", None) else []
    tags = {
        "TITLE": one(t.title), "ARTIST": one(t.artist),
        "ARTISTS": t.artists if len(t.artists) > 1 else [],
        "ALBUM": one(album.title), "ALBUMARTIST": one(album.artist), "ALBUMARTISTSORT": one(album.artist_sort),
        "TRACKNUMBER": one(n), "TRACKTOTAL": one(len(album.tracks)), "TOTALTRACKS": one(len(album.tracks)),
        "DISCNUMBER": one(album.disc), "DISCTOTAL": one(album.disc_total), "TOTALDISCS": one(album.disc_total),
        "DATE": one(album.date), "ORIGINALDATE": one(album.original_date),
        "ORIGINALYEAR": one(album.original_date[:4]), "GENRE": album.genres,
        "LABEL": one(album.label), "CATALOGNUMBER": one(album.catalog), "BARCODE": one(album.barcode),
        "MEDIA": one(album.media), "RELEASECOUNTRY": one(album.country),
        "RELEASESTATUS": one(album.status), "RELEASETYPE": one(album.release_type),
        "COMPILATION": one(1) if album.various else [],
        "MUSICBRAINZ_ALBUMID": one(album.mbid), "MUSICBRAINZ_RELEASEGROUPID": one(album.group_id),
        "MUSICBRAINZ_ALBUMARTISTID": album.artist_ids, "MUSICBRAINZ_ARTISTID": t.artist_ids,
        "MUSICBRAINZ_TRACKID": one(t.recording_id), "MUSICBRAINZ_RELEASETRACKID": one(t.track_id),
    }
    tags.update(_kept(cfg, info))
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


def _same(a: Path, b: Path) -> bool:
    try:
        return a.exists() and b.exists() and os.path.samefile(a, b)
    except OSError:
        return False


def process_folder(cfg: dict, folder: Path, *, move: bool = False, dry_run: bool = False,
                   forced_release: str | None = None, rip: RipResult | None = None,
                   no_cover: bool = False) -> list[Outcome]:
    """Process every album found under `folder`. One album = one directory (+ disc number)."""
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
    mb = MusicBrainz(cfg)
    return [_process_album(cfg, mb, g, infos, move=move, dry_run=dry_run, forced_release=forced_release,
                           rip=rip, no_cover=no_cover) for g in groups.values()]


def _process_album(cfg, mb, files, infos, *, move, dry_run, forced_release, rip, no_cover) -> Outcome:
    src_dir = files[0].parent
    out = Outcome(source=src_dir)
    say = out.report.append
    stamp = datetime.now().strftime("%Y-%m-%d %H.%M.%S")
    say(f"ripflow report  {stamp}")
    say(f"source: {src_dir}  ({len(files)} files, {'ripped from CD' if rip else 'existing files'})")

    # 1. integrity -------------------------------------------------------------
    checks = check_many(files, pick_decoder(cfg), cfg["verify"]["jobs"])
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

    txt_id, txt_toc = _read_discid_txt(src_dir)
    tag_ids = {i.tag("MUSICBRAINZ_DISCID", "DISCID") for i in flacs}
    discid = (rip.discid if rip else None) or txt_id or (tag_ids.pop() if len(tag_ids) == 1 else "") or None
    cd_like = all(i.sample_rate == 44100 and i.bps == 16 and i.channels == 2 for i in flacs)
    toc = txt_toc or (toc_from_samples([i.total_samples for i in flacs]) if cd_like else None)
    say(f"\n== Identification ==\n  disc id: {discid or (toc.discid + ' (computed from track lengths)' if toc else 'n/a')}")
    if toc:
        say(f"  toc: {toc.toc_param}")

    # A folder that still carries our discid.txt / rip log is one of our own CD rips.
    from_cd = bool(rip or txt_id or any(src_dir.glob("rip*.log")))
    say(f"  treated as: {'CD rip (prefer CD releases)' if from_cd else 'existing files (prefer Digital Media)'}")
    ident = identify(mb, cfg, flacs, discid=discid, toc=toc, forced_release=forced_release,
                     source="rip" if from_cd else "files")
    say(f"  method: {ident.method}")
    for line in ident.candidates + [f"  note: {n}" for n in ident.notes]:
        say("  " + line)

    naming = cfg["naming"]
    album = ident.album
    have_tags = not rip and all(i.tag("ALBUM") and i.tag("TITLE") for i in flacs)
    unknown = False
    if album:
        artist, title, year = album.artist, album.title, (album.original_date or album.date)[:4]
        disc, disc_total, various = album.disc, album.disc_total, album.various
        items = [Item(p, i, n, album.tracks[n - 1].title, _album_tags(cfg, album, n, i, discid))
                 for n, (p, i) in enumerate(zip(files, flacs), 1)]
        out.label = f"{artist} - {title}"
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

    fields = {"albumartist": safe_name(artist), "album": safe_name(title), "year": year or "0000"}
    dest_dir = Path(cfg["paths"]["library"]).joinpath(
        *[safe_name(part.format(**fields)) for part in naming["album_dir"].split("/")])
    pattern = naming["track_multidisc"] if disc_total > 1 else naming["track"]
    targets = [dest_dir / (safe_name(pattern.format(track=it.track, disc=disc, title=it.title,
                                                    artist=artist, album=title)) + ".flac") for it in items]
    out.dest = dest_dir
    say(f"\n== Destination ==\n  {dest_dir}")
    for it, tgt in zip(items, targets):
        say(f"  {it.src.name}  ->  {tgt.name}")

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
    cover = None
    if cfg["cover"]["enabled"] and not no_cover and not unknown:
        cover, notes = find_cover(cfg, artist=artist, album=title, various=various, ntracks=len(items),
                                  mbid=album.mbid if album else "", group_id=album.group_id if album else "",
                                  local_dirs=[dest_dir, src_dir])
        say("\n== Cover art ==")
        for n in notes:
            say("  " + n)
        if cover is None:
            out.problems.append("no usable cover art found")
        else:
            say(f"  chosen: {cover.source} {cover.width}x{cover.height}")
            if cover.size < cfg["cover"]["min_size"]:
                out.problems.append(f"cover art is only {cover.width}x{cover.height} ({cover.source})")

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
    for it, tgt in zip(items, targets):
        if not _same(it.src, tgt):
            (shutil.move if move else shutil.copy2)(str(it.src), str(tgt))
        if it.tags is not None or cover:
            write_tags(tgt, it.tags if it.tags is not None else it.info.tags,
                       picture=cover.embed if cover else None)
            after = read_info(tgt)
            if (after.md5, after.total_samples) != (it.info.md5, it.info.total_samples):
                out.problems.append(f"header changed unexpectedly after tagging: {tgt.name}")
    if cover:
        (dest_dir / "cover.jpg").write_bytes(cover.jpeg)
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
    if move and not rip and not _same(src_dir, dest_dir):
        for extra in src_dir.glob("rip*.log"):          # carry earlier rip logs along
            if not (dest_dir / extra.name).exists():
                shutil.move(str(extra), str(dest_dir / extra.name))
    if move and not _same(src_dir, dest_dir) and src_dir.is_dir() and not any(src_dir.iterdir()):
        src_dir.rmdir()
    return _finish(cfg, out, dry_run)


def _finish(cfg: dict, out: Outcome, dry_run: bool) -> Outcome:
    out.report.append("\n== Result ==")
    out.report += [f"  ATTENTION: {p}" for p in out.problems] or ["  all good"]
    text = "\n".join(out.report) + "\n"
    if dry_run:
        print(text)
        return out
    reports = Path(cfg["paths"]["logs"]) / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    name = safe_name(f"{datetime.now():%Y-%m-%d %H.%M.%S} {out.label or out.source.name}", 150) + ".txt"
    (reports / name).write_text(text, encoding="utf-8")
    where = out.dest or out.source
    log.info("%s -> %s (%s)", out.label or out.source.name, where,
             "OK" if out.ok else f"{len(out.problems)} issue(s)")
    for p in out.problems:
        attention(f"{where} :: {p}")
    return out
