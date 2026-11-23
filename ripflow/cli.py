"""Command line entry point:  python -m ripflow <command>"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import load, setup_logging

SKIPPED = 3     # exit code of `process --ask` when you skipped (s) every album


def _art_options(p: argparse.ArgumentParser) -> None:
    art = p.add_mutually_exclusive_group()
    art.add_argument("--no-cover", action="store_true", help="leave cover art alone")
    art.add_argument("--ignore-local-art", action="store_true",
                     help="ignore cover.jpg / embedded art already present and look online again")
    art.add_argument("--only-local-art", action="store_true",
                     help="use only a cover/folder/front .jpg/.png/.webp in the album folder (newest wins); "
                          "no online lookups, embedded art ignored and replaced")
    p.add_argument("--show-art", action="store_true",
                   help="with the before/after table, also open a before/after picture of the cover art")
    p.add_argument("--prefer-discid", action="store_true",
                   help="identify by the disc ID (recorded, or computed from CD-style files) before the "
                        "files' MUSICBRAINZ_ALBUMID tag; the tag is used if MusicBrainz knows no release "
                        "for the disc ID")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ripflow", description=__doc__)
    ap.add_argument("--config", help="path to ripflow.toml (default: ./ripflow.toml)")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("watch", help="wait for audio CDs; rip + process each one, then eject")

    p = sub.add_parser("rip", help="rip the disc that is in the drive right now, then process it")
    p.add_argument("--drive", help="drive letter (F:) or device; default from config / auto")

    p = sub.add_parser("process", help="verify, identify, tag and file a folder of FLACs")
    p.add_argument("folder", type=Path)
    p.add_argument("--move", action="store_true", help="move files into the library (default: copy)")
    p.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    p.add_argument("--release", help="force a specific MusicBrainz release id")
    p.add_argument("--auto", action="store_true",
                   help="never ask which release to use; always take the recommended one")
    p.add_argument("--ask", action="store_true",
                   help="before changing anything, show a before/after table and ask: y = go ahead, "
                        f"n = wrong release (pick another), s = skip the album (exit code {SKIPPED} "
                        "if every album was skipped)")
    _art_options(p)

    p = sub.add_parser("discids", help="record the disc IDs in an existing library's rip logs (cyanrip, "
                                       "EAC, XLD, ripflow's own) in each album's ripflow.json, for "
                                       "later process / review runs")
    p.add_argument("root", type=Path, help="library, artist or album folder")
    p.add_argument("--dry-run", action="store_true", help="only show what would be recorded")

    p = sub.add_parser("review", help="go through an existing library album by album: process each "
                                      "one with a before/after check (process --ask), remembering "
                                      "which are done in each album's ripflow.json")
    p.add_argument("root", type=Path, help="library, artist or album folder to go through")
    p.add_argument("--move", action="store_true", help="move files into the library (default: copy)")
    p.add_argument("--include-skipped", action="store_true",
                   help="also go through albums you skipped (s) in an earlier review")
    p.add_argument("--include-completed", action="store_true",
                   help="also go through albums an earlier review applied or found unchanged")
    p.add_argument("--limit", type=int, default=0, help="stop after N albums (for a test)")
    _art_options(p)

    p = sub.add_parser("covers", help="update cover art across an existing library (animated covers, "
                                      "missing stills, still naming); audio files are not modified")
    p.add_argument("root", type=Path)
    p.add_argument("--dry-run", action="store_true", help="look things up, write nothing")
    p.add_argument("--recheck", action="store_true",
                   help="also look again at albums an earlier run found no animated cover for")
    p.add_argument("--limit", type=int, default=0, help="only the first N album folders (for a test)")

    p = sub.add_parser("verify", help="integrity-scan a whole library (check_flac.sh replacement)")
    p.add_argument("root", type=Path)
    p.add_argument("--out", type=Path, default=Path("flac_report.tsv"))
    p.add_argument("--jobs", type=int, default=0)

    a = ap.parse_args(argv)
    cfg = load(a.config)
    setup_logging(cfg, a.verbose)

    if a.cmd == "verify":
        from .verify import pick_decoder, scan_library
        return 1 if scan_library(a.root, pick_decoder(cfg), a.out, a.jobs or cfg["verify"]["jobs"]) else 0
    if a.cmd == "covers":
        from .library import update_covers
        return update_covers(cfg, a.root, dry_run=a.dry_run, recheck=a.recheck, limit=a.limit)
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if a.cmd == "process":
        from .pipeline import process_folder
        from .prompt import ask_release
        if a.ask and not interactive:
            raise SystemExit("--ask needs a terminal to answer at")
        outcomes = process_folder(cfg, a.folder, move=a.move, dry_run=a.dry_run,
                                  forced_release=a.release, no_cover=a.no_cover,
                                  ignore_local_art=a.ignore_local_art, only_local_art=a.only_local_art,
                                  chooser=None if (a.auto and not a.ask) or not interactive else ask_release,
                                  confirm=a.ask, show_art=a.show_art, prefer_discid=a.prefer_discid)
        if a.ask and outcomes and all(o.decision == "skipped" for o in outcomes):
            return SKIPPED
        return 0 if all(o.ok for o in outcomes) else 2
    if a.cmd == "discids":
        from .disclog import record_library
        return record_library(a.root, dry_run=a.dry_run)
    if a.cmd == "review":
        from .review import review
        if not interactive:
            raise SystemExit("review is attended: run it in a terminal")
        return review(cfg, a.root, move=a.move, include_skipped=a.include_skipped,
                      include_completed=a.include_completed, limit=a.limit,
                      no_cover=a.no_cover, ignore_local_art=a.ignore_local_art,
                      only_local_art=a.only_local_art, show_art=a.show_art, prefer_discid=a.prefer_discid)
    from . import watch
    if a.cmd == "watch":
        watch.watch(cfg)
        return 0
    drives = [a.drive] if a.drive else [d for d in watch.cd_drives(cfg) if watch.disc_signature(d)]
    if not drives:
        print("no audio CD found")
        return 1
    return 0 if watch.rip_and_process(cfg, drives[0]) else 2
