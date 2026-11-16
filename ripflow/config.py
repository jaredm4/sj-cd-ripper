"""Config loading (TOML) with defaults, plus logging setup."""
from __future__ import annotations

import copy
import logging
import os
import tomllib
from pathlib import Path

from . import __version__

DEFAULTS: dict = {
    "paths": {
        "library": "",   # required: where finished albums go (your Jellyfin music folder)
        "staging": "",   # default: <config dir>/work
        "logs": "",      # default: <config dir>/logs
    },
    "tools": {"cyanrip": "cyanrip", "flac": "flac", "ffmpeg": "ffmpeg", "rsgain": "rsgain"},
    "rip": {
        "drive": "",            # "" = any drive holding an audio CD
        "drive_offset": "",     # read offset in samples, e.g. 6 (same number EAC uses)
        # {drive} and {offset} are substituted; the "-s" pair is dropped if no offset is set.
        "args": ["-d", "{drive}", "-s", "{offset}", "-N", "-o", "flac", "-D", "rip"],
        "timeout_minutes": 180,
        "poll_seconds": 5,
        "eject": "always",      # always | success | never
        "beep": True,           # Windows only: 1 beep = fine, 3 = needs attention
    },
    "verify": {"jobs": 0},      # 0 = CPU count
    "musicbrainz": {
        "contact": "",          # your email or URL; MusicBrainz asks for one in the User-Agent
        "prefer_countries": ["US", "XW", "XE"],   # most preferred first; everything else after
        "avoid_countries": ["JP", "CN"],          # only used when nothing else fits
        "prefer_explicit": True,                  # unless the files carry a Rating=Clean tag
        "max_track_diff_seconds": 7,
        "genres": 3,
    },
    "cover": {
        "enabled": True,
        "sources": ["local", "itunes"],                      # largest image from these wins
        "fallback_sources": ["coverartarchive", "deezer"],   # only if none of the above has one
        "max_aspect": 1.15,     # up to this far from square is centre-cropped; beyond it, skipped
        "itunes_country": "US",
        "min_size": 600,        # the largest image found wins, but it must be at least this
        "max_size": 3000,       # larger images are scaled down to this
        "jpeg_quality": 88,
        "embed_size": 600,
        "embed_quality": 85,
    },
    "tags": {
        # Existing tags that survive a retag (everything else is replaced by MusicBrainz data).
        # A trailing * matches a prefix.
        "keep": ["ALBUM DYNAMIC RANGE", "DYNAMIC RANGE", "ISRC", "LYRICS", "UNSYNCEDLYRICS",
                 "MUSICBRAINZ_DISCID", "UPC"],
    },
    "dr": {"enabled": True, "write_tags": True, "write_log": True},     # dynamic range (DR14)
    "rsgain": {"enabled": True, "args": ["easy", "--output", "{album}"]},
    "naming": {
        "album_dir": "{albumartist}/{album}",
        "track": "{albumartist} - {album} - {track:02d} {title}",
        "track_multidisc": "{albumartist} - {album} - {disc}-{track:02d} {title}",
        "unknown_artist": "Unknown",
    },
}


def _merge(base: dict, extra: dict) -> dict:
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def find_config(explicit: str | None) -> Path | None:
    candidates = [explicit, os.environ.get("RIPFLOW_CONFIG"), "ripflow.toml",
                  str(Path(__file__).resolve().parent.parent / "ripflow.toml")]
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c).resolve()
    if explicit:
        raise SystemExit(f"config file not found: {explicit}")
    return None


def load(explicit: str | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    path = find_config(explicit)
    base = path.parent if path else Path.cwd()
    if path:
        with open(path, "rb") as f:
            _merge(cfg, tomllib.load(f))
    p = cfg["paths"]
    p["library"] = Path(p["library"]).expanduser() if p["library"] else None
    p["staging"] = Path(p["staging"]).expanduser() if p["staging"] else base / "work"
    p["logs"] = Path(p["logs"]).expanduser() if p["logs"] else base / "logs"
    cfg["_config_path"] = path
    contact = cfg["musicbrainz"]["contact"] or "https://github.com/ (no contact set)"
    cfg["_user_agent"] = f"ripflow/{__version__} ( {contact} )"
    return cfg


log = logging.getLogger("ripflow")
attention_log = logging.getLogger("ripflow.attention")


def setup_logging(cfg: dict, verbose: bool = False) -> None:
    logs: Path = cfg["paths"]["logs"]
    logs.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger("ripflow")
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    main = logging.FileHandler(logs / "ripflow.log", encoding="utf-8")
    main.setFormatter(fmt)
    root.addHandler(console)
    root.addHandler(main)
    att = logging.FileHandler(logs / "ATTENTION.log", encoding="utf-8")
    att.setFormatter(fmt)
    attention_log.handlers.clear()
    attention_log.addHandler(att)


def attention(msg: str) -> None:
    """Something a human should look at. Goes to ATTENTION.log (and the main log)."""
    attention_log.warning(msg)
