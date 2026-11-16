"""Rip one disc with cyanrip and read its AccurateRip verdicts back out of the log."""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import log
from .util import natural_key


@dataclass
class TrackRip:
    number: int
    state: str            # accurate | mismatch | unverified | failed
    confidence: int = 0
    detail: str = ""


@dataclass
class RipResult:
    folder: Path
    files: list[Path] = field(default_factory=list)
    log_path: Path | None = None
    discid: str | None = None
    tracks: list[TrackRip] = field(default_factory=list)
    status: str = "UNKNOWN"   # VERIFIED | UNVERIFIED | SUSPECT | FAILED | UNKNOWN
    message: str = ""


_AR = re.compile(r"Accurip v([12]):\s+([0-9A-Fa-f]{8})\s+\(([^)]*)\)")


def parse_log(text: str) -> tuple[str | None, list[TrackRip], str]:
    """Parse a cyanrip log. Returns (disc id, per-track results, album status)."""
    m = re.search(r"(?im)^\s*DiscID:\s*(\S{28})\s*$", text)
    discid = m.group(1) if m else None
    parts = re.split(r"(?m)^Track (\d+) ", text)
    tracks = []
    for num, block in zip(parts[1::2], parts[2::2]):
        block = block or " "
        results = _AR.findall(block)
        good = [r for r in results if r[2].lower().startswith("accurately ripped")]
        detail = "; ".join(f"v{v} {crc} {txt}" for v, crc, txt in results)
        if "successfully" not in block.splitlines()[0]:
            tracks.append(TrackRip(int(num), "failed", detail=block.splitlines()[0].strip()))
        elif good:
            conf = max(int(c) for r in good for c in re.findall(r"confidence (\d+)", r[2]) or ["0"])
            tracks.append(TrackRip(int(num), "accurate", conf, detail))
        elif re.search(r"Accurip:\s+disc found", block):
            if "partially accurately ripped" in block:
                detail += "; partial (450-frame) match only"
            tracks.append(TrackRip(int(num), "mismatch", detail=detail))
        else:
            tracks.append(TrackRip(int(num), "unverified", detail=detail or "disc not in AccurateRip"))
    states = {t.state for t in tracks}
    if not tracks:
        status = "UNKNOWN"
    elif "failed" in states:
        status = "FAILED"
    elif "mismatch" in states:
        status = "SUSPECT"
    elif states == {"accurate"}:
        status = "VERIFIED"
    else:
        status = "UNVERIFIED"
    return discid, tracks, status


def rip_disc(cfg: dict, drive: str) -> RipResult:
    r = cfg["rip"]
    exe = shutil.which(cfg["tools"]["cyanrip"])
    folder = Path(cfg["paths"]["staging"]) / datetime.now().strftime("%Y-%m-%d %H.%M.%S")
    folder.mkdir(parents=True, exist_ok=True)
    result = RipResult(folder)
    if not exe:
        result.status, result.message = "FAILED", "cyanrip not found - set [tools] cyanrip in the config"
        return result
    offset = str(r["drive_offset"]).strip()
    args, skip = [], False
    for a in r["args"]:
        if skip:
            skip = False
            continue
        if a == "-s" and not offset:
            skip = True                      # no offset configured: drop "-s {offset}"
            continue
        args.append(str(a).replace("{drive}", drive).replace("{offset}", offset))
    cmd = [exe, *args]
    log.info("ripping %s: %s", drive, " ".join(cmd))
    console = folder / "cyanrip-console.txt"
    try:
        with open(console, "wb") as out:
            code = subprocess.run(cmd, cwd=folder, stdout=out, stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL, timeout=r["timeout_minutes"] * 60).returncode
    except subprocess.TimeoutExpired:
        code, result.message = -1, f"cyanrip timed out after {r['timeout_minutes']} minutes"
    result.files = sorted((p for p in folder.rglob("*") if p.suffix.lower() == ".flac"),
                          key=lambda p: natural_key(p.name))
    logs = [p for p in folder.rglob("*.log")]
    text = ""
    if logs:
        result.log_path = logs[0]
        text = logs[0].read_text(encoding="utf-8", errors="replace")
    result.discid, result.tracks, result.status = parse_log(text)
    if code != 0 or not result.files:
        result.status = "FAILED"
        result.message = result.message or f"cyanrip exited with code {code}; see {console}"
    elif len(result.tracks) != len(result.files):
        result.message = (f"rip log lists {len(result.tracks)} tracks but {len(result.files)} files "
                          "were written - AccurateRip status may be incomplete")
        if result.status == "VERIFIED":
            result.status = "UNKNOWN"
    log.info("rip finished: %s, %d files, disc id %s", result.status, len(result.files), result.discid)
    return result
