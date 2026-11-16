"""Dynamic range (DR14, the "TT DR Offline Meter" / foobar2000 DR Meter measure).

Per channel: cut the audio into 3-second blocks, take RMS and peak of each block,
then DR = 20*log10(second-highest peak / RMS of the loudest 20% of blocks).
Track DR is the channel average, album DR the average of the track values.
Audio is streamed block by block, so memory use stays small.
"""
from __future__ import annotations

import math
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .flacfile import FlacInfo
from .verify import decode_cmd


@dataclass
class TrackDR:
    dr: int
    peak_db: float
    rms_db: float
    seconds: float


def _to_float(np, raw: bytes, width: int, channels: int):
    if width == 3:
        a = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = a[:, 0] | (a[:, 1] << 8) | (a[:, 2] << 16)
        v = (v ^ 0x800000) - 0x800000
    else:
        v = np.frombuffer(raw, dtype={1: "i1", 2: "<i2", 4: "<i4"}[width])
    return v.reshape(-1, channels).astype(np.float64) / float(1 << (8 * width - 1))


def measure_track(info: FlacInfo, decoder: tuple[str, str]) -> TrackDR:
    import numpy as np
    cmd, width, _ = decode_cmd(info, decoder)
    fs, ch = info.sample_rate, info.channels
    block = 3 * (fs + (60 if fs == 44100 else 0))      # 132480 samples at 44.1 kHz, as the TT meter uses
    frame = width * ch
    rms, peaks, total_sq, total_n = [], [], np.zeros(ch), 0
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    try:
        while True:
            raw = proc.stdout.read(block * frame)
            raw = raw[:len(raw) - len(raw) % frame]
            if not raw:
                break
            x = _to_float(np, raw, width, ch)
            sq = (x * x).sum(axis=0)
            rms.append(np.sqrt(2.0 * sq / len(x)))
            peaks.append(np.abs(x).max(axis=0))
            total_sq += sq
            total_n += len(x)
    finally:
        proc.stdout.close()
        proc.wait()
    if not rms:
        return TrackDR(0, -math.inf, -math.inf, 0.0)
    rms, peaks = np.array(rms), np.array(peaks)          # (blocks, channels)
    top = max(1, int(len(rms) * 0.2))
    loud = np.sqrt((np.sort(rms, axis=0)[::-1][:top] ** 2).sum(axis=0) / top)
    second = np.sort(peaks, axis=0)[::-1][min(1, len(peaks) - 1)]
    per_channel = [20 * math.log10(p / r) if r > 0 and p > 0 else 0.0 for p, r in zip(second, loud)]
    db = lambda v: 20 * math.log10(v) if v > 0 else -math.inf
    return TrackDR(dr=int(round(sum(per_channel) / ch)), peak_db=db(float(peaks.max())),
                   rms_db=db(float(np.sqrt(2.0 * total_sq / total_n).mean())), seconds=total_n / fs)


def measure_album(infos: list[FlacInfo], decoder: tuple[str, str], jobs: int = 0) -> tuple[int, list[TrackDR]]:
    with ThreadPoolExecutor(max_workers=jobs or None) as pool:
        tracks = list(pool.map(lambda i: measure_track(i, decoder), infos))
    return int(round(sum(t.dr for t in tracks) / len(tracks))), tracks


def write_log(path: Path, artist: str, album: str, album_dr: int, rows: list[tuple[str, TrackDR]]) -> None:
    line = "-" * 80
    out = ["ripflow dynamic range log (DR14 method)", line, f"Analyzed: {artist} / {album}", line, "",
           "DR         Peak         RMS     Duration Track", line]
    for name, t in rows:
        m, s = divmod(int(round(t.seconds)), 60)
        out.append(f"DR{t.dr:<5} {t.peak_db:8.2f} dB {t.rms_db:8.2f} dB {m:6d}:{s:02d} {name}")
    out += [line, "", f"Number of tracks:  {len(rows)}", f"Official DR value: DR{album_dr}", ""]
    path.write_text("\n".join(out), encoding="utf-8")
