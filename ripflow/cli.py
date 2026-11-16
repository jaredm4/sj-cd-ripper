"""Command line entry point:  python -m ripflow <command>"""
from __future__ import annotations

import argparse
from pathlib import Path

from . import __version__
from .config import load, setup_logging


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
    p.add_argument("--no-cover", action="store_true")

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
    if a.cmd == "process":
        from .pipeline import process_folder
        outcomes = process_folder(cfg, a.folder, move=a.move, dry_run=a.dry_run,
                                  forced_release=a.release, no_cover=a.no_cover)
        return 0 if all(o.ok for o in outcomes) else 2
    from . import watch
    if a.cmd == "watch":
        watch.watch(cfg)
        return 0
    drives = [a.drive] if a.drive else [d for d in watch.cd_drives(cfg) if watch.disc_signature(d)]
    if not drives:
        print("no audio CD found")
        return 1
    return 0 if watch.rip_and_process(cfg, drives[0]) else 2
