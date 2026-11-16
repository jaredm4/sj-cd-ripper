"""FLAC integrity check - a port of check_flac.sh, plus an audio MD5 comparison.

Every file is fully decoded to a pipe. We compare the number of samples actually
decoded against STREAMINFO, capture decoder errors, and compare the MD5 of the
decoded PCM against the MD5 the encoder stored in the header.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .flacfile import FlacError, read_info

BAD = {"UNREADABLE", "TRUNCATED", "LONGER_THAN_HEADER", "DECODE_ERROR", "MD5_MISMATCH"}


@dataclass
class Check:
    path: Path
    status: str                 # OK | NO_LENGTH | one of BAD
    expected: int = 0
    decoded: int = 0
    missing_seconds: float = 0.0
    md5: str = "-"              # ok | mismatch | unset | skipped
    errors: str = ""

    @property
    def ok(self) -> bool:
        return self.status not in BAD

    @property
    def missing(self) -> int:
        return self.expected - self.decoded

    def row(self) -> str:
        has_len = self.status not in ("NO_LENGTH", "UNREADABLE")
        return "\t".join(str(x) for x in (
            self.status, self.expected, self.decoded,
            self.missing if has_len else "-",
            f"{self.missing_seconds:.3f}" if has_len else "-",
            self.md5, self.errors or "-", self.path))


HEADER = "status\texpected_samples\tdecoded_samples\tmissing_samples\tmissing_seconds\tmd5\terrors\tfile"


def pick_decoder(cfg: dict) -> tuple[str, str]:
    """Prefer the reference flac decoder; fall back to ffmpeg."""
    for kind in ("flac", "ffmpeg"):
        exe = shutil.which(cfg["tools"][kind])
        if exe:
            return kind, exe
    raise SystemExit("neither flac nor ffmpeg found - install flac (or set [tools] paths)")


def decode_cmd(info, decoder: tuple[str, str]) -> tuple[list[str], int, bool]:
    """Command that writes signed little-endian PCM to stdout.
    Returns (cmd, bytes per sample, whether the output matches what FLAC's MD5 covers)."""
    kind, exe = decoder
    if kind == "flac":
        return ([exe, "-d", "-c", "-F", "-s", "--force-raw-format", "--endian=little",
                 "--sign=signed", str(info.path)], (info.bps + 7) // 8, True)
    fmt, width = {8: ("s8", 1), 16: ("s16le", 2), 24: ("s24le", 3)}.get(info.bps, ("s32le", 4))
    return ([exe, "-v", "error", "-nostdin", "-i", str(info.path), "-map", "0:a:0",
             "-c:a", "pcm_" + fmt, "-f", fmt, "-"], width, info.bps in (8, 16, 24))


def check_flac(path: Path, decoder: tuple[str, str]) -> Check:
    path = Path(path)
    try:
        info = read_info(path)
    except (FlacError, OSError) as e:
        return Check(path, "UNREADABLE", errors=str(e))
    cmd, width, md5_comparable = decode_cmd(info, decoder)
    md5, nbytes = hashlib.md5(), 0
    with tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, stdin=subprocess.DEVNULL)
        except OSError as e:
            return Check(path, "UNREADABLE", errors=f"could not run decoder: {e}")
        while chunk := proc.stdout.read(1 << 20):
            md5.update(chunk)
            nbytes += len(chunk)
        proc.wait()
        err.seek(0)
        lines = err.read().decode("utf-8", "replace").splitlines()
    prefix = f"{path}: "
    errs = " ".join(" ".join(l.replace(prefix, "").split()) for l in lines
                    if l.strip() and "md5 signature since it was unset" not in l.lower())
    c = Check(path, "OK", expected=info.total_samples,
              decoded=nbytes // (info.channels * width), errors=errs)
    if int(info.md5, 16) == 0:
        c.md5 = "unset"
    elif not md5_comparable:
        c.md5 = "skipped"
    else:
        c.md5 = "ok" if md5.hexdigest() == info.md5 else "mismatch"
    if info.total_samples == 0:
        c.status = "DECODE_ERROR" if errs else "NO_LENGTH"
        return c
    c.missing_seconds = c.missing / info.sample_rate
    if c.missing > 0:
        c.status = "TRUNCATED"
    elif c.missing < 0:
        c.status = "LONGER_THAN_HEADER"
    elif errs:
        c.status = "DECODE_ERROR"
    elif c.md5 == "mismatch":
        c.status = "MD5_MISMATCH"
    return c


def check_many(paths: list[Path], decoder: tuple[str, str], jobs: int = 0) -> list[Check]:
    jobs = jobs or os.cpu_count() or 4
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        return list(pool.map(lambda p: check_flac(p, decoder), paths))


def scan_library(root: Path, decoder: tuple[str, str], out: Path, jobs: int = 0) -> int:
    """Whole-library scan, same idea and output shape as check_flac.sh. Returns bad-file count."""
    files = sorted(p for p in Path(root).rglob("*") if p.suffix.lower() == ".flac" and p.is_file())
    print(f"Checking {len(files)} FLAC files under '{root}' with {decoder[0]}...")
    results = check_many(files, decoder, jobs)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(HEADER + "\n")
        f.writelines(c.row() + "\n" for c in results)
    counts: dict[str, int] = {}
    albums: dict[Path, list[Check]] = {}
    for c in results:
        counts[c.status] = counts.get(c.status, 0) + 1
        albums.setdefault(c.path.parent, []).append(c)
    print("\n== Files by status ==")
    for s, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"{n:7d} {s}")
    print("\n== Albums with problems (bad tracks / total tracks, worst gap) ==")
    for d, cs in sorted(albums.items()):
        bad = [c for c in cs if c.status != "OK"]
        if bad:
            worst = max(c.missing_seconds for c in bad)
            print(f"{len(bad):3d}/{len(cs):<3d}  worst {worst:8.3f}s  {d}")
    print(f"\nFull per-file results: {out}")
    return sum(1 for c in results if not c.ok)
