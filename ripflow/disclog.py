"""Disc IDs from rip logs, and ripflow.json's record of them.

A disc ID read from the disc itself (or rebuilt from a rip log's exact sector table) identifies
the CD on MusicBrainz exactly, also for Enhanced CDs and discs with a hidden first track, where
an ID computed from the files' lengths comes out wrong. ripflow.json keeps it per disc:

    "discs": {"1": {"discid": "...", "toc": "1+12+257193+150+...", "mcn": "075021036789",
                    "from": "rip.log", "recorded": "2026-11-25"}}

"mcn" is the barcode the disc itself carries (only when the rip log shows one; never one taken
from MusicBrainz). Read here: cyanrip logs (rip.log, rip-disc2.log...), ripflow's discid.txt and
reports, and EAC / XLD logs (their "TOC of the extracted CD" table).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .discid import Toc, toc_from_param

SECTOR = 588                 # samples per CD sector (2352 bytes / 4)
DATA_GAP = 11250             # an Enhanced CD's data session starts ~11400 sectors after the audio
KEY = "discs"


@dataclass
class DiscLog:
    toc: Toc
    source: str              # file it came from
    disc: int | None = None  # disc number the file name says (rip-disc2.log), if any
    mcn: str = ""

    def entry(self, today: str) -> dict:
        e = {"discid": self.toc.discid, "toc": self.toc.toc_param, "from": self.source, "recorded": today}
        if self.mcn:
            e["mcn"] = self.mcn
        return e


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or raw[1:2] == b"\x00":     # EAC writes UTF-16
        return raw.decode("utf-16", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _toc(starts: list[int], ends: list[int]) -> Toc | None:
    """MusicBrainz TOC from audio tracks' start/end sectors (LSN, 0 = first sector of the
    program area). A last track far beyond the others is an Enhanced CD's data track: left out,
    as MusicBrainz does, the audio session's end becoming the lead-out."""
    if not starts or len(starts) != len(ends):
        return None
    if len(starts) > 1 and starts[-1] - ends[-2] - 1 >= DATA_GAP:
        starts, ends = starts[:-1], ends[:-1]
    if any(b <= a for a, b in zip(starts, starts[1:])):
        return None
    return Toc(1, len(starts), ends[-1] + 1 + 150, [s + 150 for s in starts])


def from_cyanrip(text: str) -> tuple[Toc | None, str, str]:
    """(TOC, disc id cyanrip logged, MCN) from a cyanrip log. The TOC is rebuilt from each
    ripped track's Start/End LSN (data tracks are not ripped, so they are not in it)."""
    m = re.search(r"(?im)^\s*DiscID:\s*(\S{28})\s*$", text)
    logged = m.group(1) if m else ""
    starts, ends = [], []
    for block in re.split(r"(?m)^Track \d+ ", text)[1:]:
        if block.lstrip().lower().startswith("is data"):
            continue
        s, e = re.search(r"Start LSN:\s+(\d+)", block), re.search(r"End LSN:\s+(\d+)", block)
        if s and e:
            starts.append(int(s.group(1)))
            ends.append(int(e.group(1)))
    # cyanrip: "Disc MCN:" in the header, "disc_mcn:" among each track's tags
    m = re.search(r"(?im)^\s*(?:disc[ _]?)?(?:mcn|barcode|upc|ean)\s*:\s*(\d{12,14})\s*$", text)
    mcn = m.group(1) if m and set(m.group(1)) != {"0"} else ""
    return _toc(starts, ends), logged, mcn


_EAC_ROW = re.compile(r"^\s*(\d+)\s*\|\s*[\d:.]+\s*\|\s*[\d:.]+\s*\|\s*(\d+)\s*\|\s*(\d+)\s*$", re.M)


def from_eac(text: str) -> Toc | None:
    """TOC from an EAC (or XLD) log's "TOC of the extracted CD" table:
    Track | Start | Length | Start sector | End sector. Only the first table (one per log)."""
    rows, last = [], 0
    for m in _EAC_ROW.finditer(text):
        n = int(m.group(1))
        if rows and n != last + 1:
            break                              # a second table or something else
        rows.append((int(m.group(2)), int(m.group(3))))
        last = n
    return _toc([r[0] for r in rows], [r[1] for r in rows]) if rows else None


def from_report(text: str) -> Toc | None:
    """The disc a ripflow report (ripflow-report.log, logs/reports) names, when its ID was not
    computed from the files (then the report adds nothing new)."""
    m = re.search(r"(?m)^\s*disc id:\s*(\S{28})(.*)$", text)
    t = re.search(r"(?m)^\s*toc:\s*([\d+]+)\s*$", text)
    if not m or "computed" in m.group(2) or not t:
        return None
    toc = toc_from_param(t.group(1))
    return toc if toc and toc.discid == m.group(1) else None


def logs_in(folder: Path) -> list[DiscLog]:
    """Every disc layout the logs in an album folder record."""
    found: list[DiscLog] = []
    for f in sorted(p for p in folder.iterdir() if p.is_file()):
        name = f.name.lower()
        try:
            if name == "rip.log" or re.fullmatch(r"rip-disc\d+\.log", name):
                toc, logged, mcn = from_cyanrip(read_text(f))
                if toc and (not logged or toc.discid == logged):
                    n = re.search(r"disc(\d+)", name)
                    found.append(DiscLog(toc, f.name, int(n.group(1)) if n else None, mcn))
            elif name == "discid.txt":
                kv = dict(l.split("=", 1) for l in read_text(f).splitlines() if "=" in l and not l.startswith("#"))
                toc = toc_from_param(kv.get("toc", "").strip())
                if toc and toc.discid == kv.get("discid", "").strip():
                    found.append(DiscLog(toc, f.name))
            elif name.startswith("ripflow-report") and name.endswith(".log"):
                toc = from_report(read_text(f))
                if toc:
                    n = re.search(r"disc(\d+)", name)
                    found.append(DiscLog(toc, f.name, int(n.group(1)) if n else None))
            elif name.endswith(".log"):
                text = read_text(f)
                if "TOC of the extracted CD" in text or ("Start sector" in text and "End sector" in text):
                    toc = from_eac(text)
                    if toc:
                        found.append(DiscLog(toc, f.name))
        except OSError:
            continue
    return found


def fits(toc: Toc, samples: list[int], slack_sectors: int = 225) -> bool:
    """Do the files (their lengths in samples, in track order) match this disc? Same number of
    tracks, each within 3 s (rippers differ in where they put the gaps between tracks)."""
    if len(samples) != len(toc.offsets):
        return False
    bounds = toc.offsets + [toc.leadout]
    return all(abs((b - a) - n / SECTOR) <= slack_sectors for a, b, n in zip(bounds, bounds[1:], samples))


def stored(memory: dict, disc: int) -> dict | None:
    """ripflow.json's record of disc number `disc`, if it has a usable one."""
    e = (memory.get(KEY) or {}).get(str(disc or 1))
    return e if isinstance(e, dict) and e.get("discid") else None


_PRIORITY = {"rip": 0, "discid.txt": 1, "ripflow-report": 2}


def _rank(d: DiscLog) -> int:
    n = d.source.lower()
    return next((v for k, v in _PRIORITY.items() if n.startswith(k)), 3)


def record_library(root: Path, dry_run: bool = False) -> int:
    """`ripflow discids <root>`: for every album folder under root, find disc layouts in its
    logs, check each against the files (same track count, lengths within 3 s), and record it in
    ripflow.json under its disc number. An existing record is never replaced."""
    from . import albumstate
    from .flacfile import FlacError, read_info
    from .review import find_albums
    from .util import lead_int, natural_key

    albums, _ = find_albums(Path(root))
    tally = {"recorded": 0, "already recorded": 0, "log does not match the files": 0,
             "differs from the recorded disc": 0}
    folders_with_logs = 0
    for folder in albums:
        logs = sorted(logs_in(folder), key=_rank)
        if not logs:
            continue
        folders_with_logs += 1
        discs: dict[int, list[tuple[int, str, int]]] = {}
        for f in folder.glob("*.flac"):
            try:
                i = read_info(f)
            except (FlacError, OSError):
                continue
            discs.setdefault(lead_int(i.tag("DISCNUMBER"), 1), []).append(
                (lead_int(i.tag("TRACKNUMBER"), 0), f.name, i.total_samples))
        samples = {n: [s for _, _, s in sorted(v, key=lambda x: (x[0], natural_key(x[1])))] for n, v in discs.items()}
        memory = albumstate.load(folder)
        record = dict(memory.get(KEY) or {})
        lines, done = [], set()
        for d in logs:
            fit = [n for n, s in samples.items() if fits(d.toc, s)]
            n = d.disc if d.disc in fit else fit[0] if len(fit) == 1 else None
            if n is None:
                tally["log does not match the files"] += 1
                lines.append(f"  {d.source}: {d.toc.discid} does not match the files here - skipped")
                continue
            have = record.get(str(n))
            if n in done:
                continue                      # another log for a disc already handled
            done.add(n)
            if have and have.get("discid") == d.toc.discid:
                tally["already recorded"] += 1
                lines.append(f"  disc {n}: {d.toc.discid} already recorded")
                continue
            if have:
                tally["differs from the recorded disc"] += 1
                lines.append(f"  disc {n}: {d.source} says {d.toc.discid}, ripflow.json has "
                             f"{have.get('discid')} (from {have.get('from', '?')}) - kept that")
                continue
            record[str(n)] = d.entry(albumstate.today())
            tally["recorded"] += 1
            lines.append(f"  disc {n}: {d.toc.discid} from {d.source}" + (f", barcode {d.mcn}" if d.mcn else "")
                         + (" (would record)" if dry_run else ""))
        if lines:
            print(f"{folder}\n" + "\n".join(lines))
        if record != (memory.get(KEY) or {}) and not dry_run:
            albumstate.save(folder, {**memory, KEY: record})
    print(f"\n{len(albums)} album folder(s), {folders_with_logs} with rip logs")
    for k, v in tally.items():
        print(f"  {v:5}  {k}")
    return 0
