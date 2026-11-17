"""Rip one disc with cyanrip and read its AccurateRip verdicts back out of the log."""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import log
from .util import NetworkError, NotFound, http_get, natural_key


@dataclass
class TrackRip:
    number: int
    state: str            # accurate | mismatch | unverified | failed
    confidence: int = 0
    detail: str = ""
    v1: int | None = None        # AccurateRip checksums cyanrip computed for this track
    v2: int | None = None
    start: int | None = None     # first / last sector (LSN) of the track on the disc
    end: int | None = None
    isrc: str = ""               # the track's ISRC as read from the disc


@dataclass
class RipResult:
    folder: Path
    files: list[Path] = field(default_factory=list)
    log_path: Path | None = None
    discid: str | None = None
    tracks: list[TrackRip] = field(default_factory=list)
    status: str = "UNKNOWN"   # VERIFIED | UNVERIFIED | SUSPECT | FAILED | UNKNOWN
    message: str = ""


_AR = re.compile(r"Accurip v([12]):\s+([0-9A-Fa-f]{8})(?:[ \t]+\(([^)]*)\))?")


def _status(tracks: list[TrackRip]) -> str:
    states = {t.state for t in tracks}
    if not tracks:
        return "UNKNOWN"
    if "failed" in states:
        return "FAILED"
    if "mismatch" in states:
        return "SUSPECT"
    return "VERIFIED" if states == {"accurate"} else "UNVERIFIED"


def parse_log(text: str) -> tuple[str | None, list[TrackRip], str]:
    """Parse a cyanrip log. Returns (disc id, per-track results, album status)."""
    m = re.search(r"(?im)^\s*DiscID:\s*(\S{28})\s*$", text)
    discid = m.group(1) if m else None
    parts = re.split(r"(?m)^Track (\d+) ", text)
    tracks = []
    for num, block in zip(parts[1::2], parts[2::2]):
        block = block or " "
        if block.lstrip().lower().startswith("is data"):
            continue                       # the data session of an Enhanced CD: nothing to rip
        results = _AR.findall(block)
        good = [r for r in results if r[2].lower().startswith("accurately ripped")]
        detail = "; ".join(f"v{v} {crc} {txt}".strip() for v, crc, txt in results)
        if "successfully" not in block.splitlines()[0]:
            t = TrackRip(int(num), "failed", detail=block.splitlines()[0].strip())
        elif good:
            conf = max(int(c) for r in good for c in re.findall(r"confidence (\d+)", r[2]) or ["0"])
            t = TrackRip(int(num), "accurate", conf, detail)
        elif re.search(r"Accurip:\s+disc found", block):
            if "partially accurately ripped" in block:
                detail += "; partial (450-frame) match only"
            t = TrackRip(int(num), "mismatch", detail=detail)
        else:
            t = TrackRip(int(num), "unverified", detail="cyanrip did not find the disc in AccurateRip")
        crcs = {v: int(crc, 16) for v, crc, _ in results}
        t.v1, t.v2 = crcs.get("1"), crcs.get("2")
        m = re.search(r"(?im)^\s*isrc:\s*([A-Z0-9-]{12,15})\s*$", block)
        t.isrc = m.group(1).upper().replace("-", "") if m else ""
        for name, attr in (("Start", "start"), ("End", "end")):
            m = re.search(rf"{name} LSN:\s+(\d+)", block)
            setattr(t, attr, int(m.group(1)) if m else None)
        tracks.append(t)
    return discid, tracks, _status(tracks)


# ---- our own AccurateRip lookup ------------------------------------------------
# cyanrip computes the checksums but its database lookup sometimes reports "not found"
# for discs that are there. The checksums in its log are all we need to ask ourselves.

def cddb_id(starts: list[int], leadout: int) -> str:
    """Classic CDDB/freedb disc id from ALL track start sectors (LSN) and the disc lead-out."""
    digits = lambda secs: sum(int(c) for c in str(secs))
    return "%08x" % ((sum(digits((s + 150) // 75) for s in starts) % 255) << 24
                     | ((leadout + 150) // 75 - (starts[0] + 150) // 75) << 8 | len(starts))


def accuraterip_url(starts: list[int], leadout: int, cddb: str | None = None) -> str:
    """Database URL for a disc, from audio-track start sectors and a lead-out sector (LSN,
    0-based). `cddb` overrides the CDDB id part: on an Enhanced CD that id also covers the
    data track, so it cannot be derived from the audio tracks alone."""
    n = len(starts)
    id1 = (sum(starts) + leadout) & 0xFFFFFFFF
    id2 = (sum(max(s, 1) * i for i, s in enumerate(starts, 1)) + leadout * (n + 1)) & 0xFFFFFFFF
    cddb = (cddb or cddb_id(starts, leadout)).lower()
    return (f"http://www.accuraterip.com/accuraterip/{id1 & 0xF:x}/{id1 >> 4 & 0xF:x}/{id1 >> 8 & 0xF:x}/"
            f"dBAR-{n:03d}-{id1:08x}-{id2:08x}-{cddb}.bin")


def parse_accuraterip(data: bytes) -> list[list[tuple[int, int]]]:
    """A dBAR file is a series of pressings: each a list of (confidence, checksum) per track."""
    pressings, pos = [], 0
    while pos + 13 <= len(data):
        n = data[pos]
        pos += 13
        if pos + 9 * n > len(data):
            break
        pressings.append([(data[pos + 9 * i], int.from_bytes(data[pos + 9 * i + 1:pos + 9 * i + 5], "little"))
                          for i in range(n)])
        pos += 9 * n
    return pressings


def verify_accuraterip(result: RipResult, ua: str, log_cddb: str | None = None,
                       layout: tuple[list[int], int] | None = None) -> None:
    """Re-check an UNVERIFIED rip against AccurateRip ourselves. Updates result in place.

    layout = (data-track start sectors, real disc lead-out) when known. On an Enhanced CD
    AccurateRip's address uses the disc's real lead-out (after the data track) and a CDDB id
    that counts the data track too, so the audio tracks alone give the wrong address."""
    ts = result.tracks
    if not ts or any(t.v1 is None or t.v2 is None or t.start is None or t.end is None for t in ts):
        return
    note = lambda text: setattr(result, "message", (result.message + " " if result.message else "") + text)
    starts, audio_end = [t.start for t in ts], ts[-1].end + 1
    urls = []
    if layout and layout[0]:
        data_starts, leadout = layout
        full = cddb_id(starts + data_starts, leadout)
        urls += [accuraterip_url(starts, leadout, full), accuraterip_url(starts, audio_end, full)]
    urls += [accuraterip_url(starts, audio_end, c) for c in (None, log_cddb)]
    pressings, tried = None, []
    for url in dict.fromkeys(urls):
        tried.append(url)
        try:
            pressings = parse_accuraterip(http_get(url, ua))
            break
        except NotFound:
            continue
        except NetworkError as e:
            note(f"ripflow's own AccurateRip lookup failed: {e}")
            return
    if pressings is None:
        note("ripflow's own lookup found no entry at: " + " or ".join(tried))
        return
    for i, t in enumerate(ts):
        conf = {v: sum(c for p in pressings if i < len(p) for c, crc in [p[i]] if crc == want)
                for v, want in (("v1", t.v1), ("v2", t.v2))}
        t.confidence = max(conf.values())
        t.state = "accurate" if t.confidence else "mismatch"
        t.detail = (f"v1 {t.v1:08X} conf {conf['v1']}; v2 {t.v2:08X} conf {conf['v2']} "
                    "(ripflow lookup, checksums from cyanrip)")
    result.status = _status(ts)
    log.info("own AccurateRip lookup: %s (%s)", result.status, url)


def _disc_layout(log_text: str, drive: str) -> tuple[list[int], int] | None:
    """(data-track start sectors, real disc lead-out), as LSNs. Read from the drive's table of
    contents (Windows); falls back to the data-track entry in the cyanrip log if it has sectors."""
    try:
        from .watch import WINDOWS, _windows_toc
        toc = _windows_toc(drive) if WINDOWS else None
        data, lsn = [], lambda pos: (toc[pos + 5] * 60 + toc[pos + 6]) * 75 + toc[pos + 7] - 150
        for pos in range(4, len(toc or b"") - 7, 8):
            if toc[pos + 2] == 0xAA:                       # lead-out entry
                return data, lsn(pos)
            if toc[pos + 1] & 0x04:                        # data track
                data.append(lsn(pos))
    except Exception as e:
        log.debug("could not read the disc layout: %r", e)
    m = re.search(r"(?ms)^Track \d+ is data.*?Start LSN:\s+(\d+).*?End LSN:\s+(\d+)", log_text)
    return ([int(m.group(1))], int(m.group(2)) + 1) if m else None


def _run_with_progress(cmd: list[str], cwd: Path, console: Path, timeout: float, every: float) -> tuple[int, bool]:
    """Run cyanrip, save everything it prints to `console`, and relay progress to our log:
    a line whenever it moves to another track, plus its latest status line every `every` seconds."""
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    timed_out = threading.Event()
    killer = threading.Timer(timeout, lambda: (timed_out.set(), proc.kill()))
    killer.start()
    pending, track, last_time = b"", None, time.monotonic()
    try:
        with open(console, "wb") as out:
            while chunk := proc.stdout.read1(4096):
                out.write(chunk)
                pending += chunk
                *lines, pending = re.split(rb"[\r\n]+", pending)     # progress lines end in \r
                for raw in lines:
                    line = " ".join(raw.decode("utf-8", "replace").split())
                    if not line:
                        continue
                    m = re.search(r"track\s+(\d+),\s*progress\s*-\s*([\d.]+)%(?:,\s*ETA\s*-\s*(.+))?", line, re.I)
                    if not m:
                        continue
                    now = time.monotonic()
                    if m.group(1) != track or (every and now - last_time >= every):
                        started = m.group(1) != track
                        track, last_time = m.group(1), now
                        eta = m.group(3) if m.group(3) and not started else ""    # the ETA is noise at a track start
                        log.info("ripping track %s: %.0f%%%s", track, float(m.group(2)), f", about {eta} left" if eta else "")
        return proc.wait(), timed_out.is_set()
    finally:
        killer.cancel()


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
    code, timed_out = _run_with_progress(cmd, folder, console, r["timeout_minutes"] * 60, r["progress_seconds"])
    if timed_out:
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
    if result.status == "UNVERIFIED":
        m = re.search(r"(?im)^\s*CDDB ID:\s*([0-9A-F]{8})\s*$", text)
        verify_accuraterip(result, cfg["_user_agent"], m.group(1) if m else None, _disc_layout(text, drive))
    log.info("rip finished: %s, %d files, disc id %s", result.status, len(result.files), result.discid)
    return result
