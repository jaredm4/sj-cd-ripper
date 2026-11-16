"""Minimal pure-Python FLAC metadata reader/writer (no third-party dependency).

Only metadata blocks are parsed or rewritten; audio frames are copied byte for byte.
Writes go to a temp file that atomically replaces the original.
"""
from __future__ import annotations

import os
import shutil
import struct
from dataclasses import dataclass, field
from pathlib import Path

STREAMINFO, PADDING, APPLICATION, SEEKTABLE, VORBIS, CUESHEET, PICTURE = range(7)


class FlacError(Exception):
    pass


@dataclass
class FlacInfo:
    path: Path
    sample_rate: int
    channels: int
    bps: int
    total_samples: int
    md5: str                      # hex; all zeros = not set by the encoder
    vendor: str = ""
    tags: dict[str, list[str]] = field(default_factory=dict)   # KEY (upper) -> values
    pictures: int = 0

    @property
    def seconds(self) -> float:
        return self.total_samples / self.sample_rate if self.sample_rate else 0.0

    def tag(self, *keys: str, default: str = "") -> str:
        for k in keys:
            v = self.tags.get(k.upper())
            if v and v[0].strip():
                return v[0].strip()
        return default


def _read_blocks(f) -> tuple[list[tuple[int, bytes]], int]:
    head = f.read(10)
    start = 0
    if head[:3] == b"ID3":      # some FLAC files carry a stray ID3v2 tag up front
        size = (head[6] & 0x7F) << 21 | (head[7] & 0x7F) << 14 | (head[8] & 0x7F) << 7 | (head[9] & 0x7F)
        start = 10 + size + (10 if head[5] & 0x10 else 0)
    f.seek(start)
    if f.read(4) != b"fLaC":
        raise FlacError("not a FLAC file (missing fLaC marker)")
    blocks = []
    while True:
        h = f.read(4)
        if len(h) < 4:
            raise FlacError("truncated inside metadata")
        n = int.from_bytes(h[1:], "big")
        data = f.read(n)
        if len(data) < n:
            raise FlacError("truncated inside metadata")
        blocks.append((h[0] & 0x7F, data))
        if h[0] & 0x80:
            break
    if not blocks or blocks[0][0] != STREAMINFO or len(blocks[0][1]) < 34:
        raise FlacError("missing STREAMINFO")
    return blocks, f.tell()


def _parse_vorbis(data: bytes) -> tuple[str, dict[str, list[str]]]:
    tags: dict[str, list[str]] = {}
    try:
        n = struct.unpack_from("<I", data, 0)[0]
        vendor = data[4:4 + n].decode("utf-8", "replace")
        pos = 4 + n
        count = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        for _ in range(count):
            n = struct.unpack_from("<I", data, pos)[0]
            pos += 4
            entry = data[pos:pos + n].decode("utf-8", "replace")
            pos += n
            if "=" in entry:
                k, v = entry.split("=", 1)
                tags.setdefault(k.upper(), []).append(v)
    except struct.error:
        return "", tags
    return vendor, tags


def read_info(path: str | os.PathLike) -> FlacInfo:
    path = Path(path)
    with open(path, "rb") as f:
        blocks, _ = _read_blocks(f)
    si = blocks[0][1]
    x = int.from_bytes(si[10:18], "big")
    info = FlacInfo(path=path, sample_rate=x >> 44, channels=((x >> 41) & 7) + 1,
                    bps=((x >> 36) & 31) + 1, total_samples=x & ((1 << 36) - 1),
                    md5=si[18:34].hex())
    if info.sample_rate == 0:
        raise FlacError("invalid STREAMINFO (sample rate 0)")
    for t, data in blocks:
        if t == VORBIS:
            info.vendor, info.tags = _parse_vorbis(data)
        elif t == PICTURE:
            info.pictures += 1
    return info


def read_picture(path: str | os.PathLike) -> bytes | None:
    """Image bytes of the embedded front cover (or, failing that, the first embedded picture)."""
    with open(path, "rb") as f:
        blocks, _ = _read_blocks(f)
    found = None
    for t, d in blocks:
        if t != PICTURE:
            continue
        try:
            kind, n = struct.unpack_from(">II", d, 0)
            pos = 8 + n
            pos += 4 + struct.unpack_from(">I", d, pos)[0] + 16
            size = struct.unpack_from(">I", d, pos)[0]
            data = d[pos + 4:pos + 4 + size]
        except struct.error:
            continue
        if kind == 3:
            return data
        found = found or data
    return found


def _vorbis_block(vendor: str, tags: dict[str, list[str]]) -> bytes:
    v = vendor.encode("utf-8")
    out = [struct.pack("<I", len(v)), v]
    entries = [f"{k.upper()}={val}".encode("utf-8")
               for k, vals in tags.items() for val in vals if val is not None and str(val) != ""]
    out.append(struct.pack("<I", len(entries)))
    for e in entries:
        out += [struct.pack("<I", len(e)), e]
    return b"".join(out)


def _picture_block(jpeg: bytes, width: int, height: int, mime: str = "image/jpeg") -> bytes:
    m = mime.encode("ascii")
    return b"".join([struct.pack(">I", 3), struct.pack(">I", len(m)), m, struct.pack(">I", 0),
                     struct.pack(">IIII", width, height, 24, 0), struct.pack(">I", len(jpeg)), jpeg])


def write_tags(path: str | os.PathLike, tags: dict[str, list[str]],
               picture: tuple[bytes, int, int] | None = None, padding: int = 4096) -> None:
    """Replace all Vorbis comments. picture=(jpeg_bytes, w, h) replaces embedded art;
    picture=None keeps whatever art is already embedded."""
    path = Path(path)
    tmp = path.with_name(path.name + ".ripflow-tmp")
    with open(path, "rb") as src:
        blocks, audio_at = _read_blocks(src)
        vendor = next((_parse_vorbis(d)[0] for t, d in blocks if t == VORBIS), "") or "ripflow"
        keep = (STREAMINFO, APPLICATION, SEEKTABLE, CUESHEET) + ((PICTURE,) if picture is None else ())
        out = [(t, d) for t, d in blocks if t in keep]
        out.append((VORBIS, _vorbis_block(vendor, tags)))
        if picture is not None:
            out.append((PICTURE, _picture_block(*picture)))
        out.append((PADDING, bytes(padding)))
        for t, d in out:
            if len(d) >= 1 << 24:
                raise FlacError("metadata block too large")
        try:
            with open(tmp, "wb") as dst:
                dst.write(b"fLaC")
                for i, (t, d) in enumerate(out):
                    dst.write(bytes([t | (0x80 if i == len(out) - 1 else 0)]) + len(d).to_bytes(3, "big") + d)
                src.seek(audio_at)
                shutil.copyfileobj(src, dst, 1 << 20)
                dst.flush()
                os.fsync(dst.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    os.replace(tmp, path)
