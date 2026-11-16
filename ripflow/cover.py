"""Cover art: find a large, confidently-matching front cover; make cover.jpg + a small embed copy."""
from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from .config import log
from .util import NetworkError, NotFound, http_get, similar


@dataclass
class Cover:
    jpeg: bytes                       # what goes into cover.jpg
    width: int
    height: int
    source: str
    embed: tuple[bytes, int, int]     # small JPEG for the FLAC picture block
    notes: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return min(self.width, self.height)


def _matches(artist: str, album: str, got_artist: str, got_album: str, various: bool) -> float:
    a = 1.0 if various or not artist else similar(artist, got_artist)
    b = similar(album, got_album)
    return (a + b) / 2 if a >= 0.8 and b >= 0.85 else 0.0


def _itunes(cfg, ua, artist, album, various, ntracks):
    term = quote(album if various else f"{artist} {album}", safe="")
    url = (f"https://itunes.apple.com/search?term={term}&media=music&entity=album&limit=25"
           f"&country={cfg['itunes_country']}")
    hits = []
    for r in json.loads(http_get(url, ua)).get("results", []):
        score = _matches(artist, album, r.get("artistName", ""), r.get("collectionName", ""), various)
        art = r.get("artworkUrl100")
        if score and art:
            score += 0.1 if r.get("trackCount") == ntracks else 0
            big = re.sub(r"/\d+x\d+[a-z]*(-\d+)?\.(jpg|png)$", f"/{cfg['max_size']}x{cfg['max_size']}bb.jpg", art)
            hits.append((score, big))
    return [u for _, u in sorted(hits, reverse=True)[:3]]


def _deezer(cfg, ua, artist, album, various, ntracks):
    q = quote(f'album:"{album}"' if various else f'artist:"{artist}" album:"{album}"')
    hits = []
    for r in json.loads(http_get(f"https://api.deezer.com/search/album?q={q}&limit=25", ua)).get("data", []):
        score = _matches(artist, album, (r.get("artist") or {}).get("name", ""), r.get("title", ""), various)
        if score and r.get("cover_xl"):
            hits.append((score + (0.1 if r.get("nb_tracks") == ntracks else 0), r["cover_xl"]))
    return [u for _, u in sorted(hits, reverse=True)[:2]]


def find_cover(cfg: dict, *, artist: str, album: str, various: bool = False, ntracks: int = 0,
               mbid: str = "", group_id: str = "", local_dirs: list[Path] = ()) -> tuple[Cover | None, list[str]]:
    """Try every configured source and keep the largest square image (ties: earlier source).
    Stops early only when an image already reaches max_size. Never raises."""
    c, ua, notes = cfg["cover"], cfg["_user_agent"], []
    try:
        from PIL import Image
    except ImportError:
        return None, ["Pillow is not installed (pip install pillow) - cover art skipped"]

    best: tuple[int, bytes, str] | None = None

    def consider(data: bytes, label: str) -> bool:
        nonlocal best
        try:
            with Image.open(io.BytesIO(data)) as im:
                w, h = im.size
        except Exception as e:
            notes.append(f"{label}: unreadable image ({e})")
            return False
        if max(w, h) > 1.1 * min(w, h):
            notes.append(f"{label}: {w}x{h} is not square, skipped")
            return False
        notes.append(f"{label}: {w}x{h}")
        if best is None or min(w, h) > best[0]:
            best = (min(w, h), data, label)
        return min(w, h) >= c["max_size"]

    def sources():
        for name in c["sources"]:
            try:
                if name == "local":
                    for d in local_dirs:
                        for f in sorted(Path(d).glob("*")):
                            if f.stem.lower() in ("cover", "folder", "front") and \
                                    f.suffix.lower() in (".jpg", ".jpeg", ".png"):
                                yield f"local file {f.name}", f.read_bytes()
                elif name == "itunes" and album:
                    for u in _itunes(c, ua, artist, album, various, ntracks):
                        yield "iTunes/Apple Music", http_get(u, ua)
                elif name == "deezer" and album:
                    for u in _deezer(c, ua, artist, album, various, ntracks):
                        yield "Deezer", http_get(u, ua)
                elif name == "coverartarchive":
                    for kind, ident in (("release", mbid), ("release-group", group_id)):
                        if ident:
                            try:
                                yield (f"Cover Art Archive ({kind})",
                                       http_get(f"https://coverartarchive.org/{kind}/{ident}/front", ua, timeout=60))
                            except NotFound:
                                pass
            except (NetworkError, NotFound, OSError, ValueError) as e:
                notes.append(f"{name}: {e}")

    for label, data in sources():
        if consider(data, label):
            break
    if best is None or best[0] < c["min_size"]:
        return None, notes

    _, data, label = best
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        original_jpeg = im.format == "JPEG" and im.mode == "RGB"
        rgb = im.convert("RGB")
    if max(rgb.size) > c["max_size"]:
        rgb.thumbnail((c["max_size"], c["max_size"]), Image.Resampling.LANCZOS)
        original_jpeg = False
    if original_jpeg:
        jpeg = data                    # already a JPEG within limits: keep it, no generation loss
    else:
        buf = io.BytesIO()
        rgb.save(buf, "JPEG", quality=c["jpeg_quality"], optimize=True)
        jpeg = buf.getvalue()
    small = rgb.copy()
    small.thumbnail((c["embed_size"], c["embed_size"]), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    small.save(buf, "JPEG", quality=c["embed_quality"], optimize=True)
    cover = Cover(jpeg, rgb.width, rgb.height, label, (buf.getvalue(), small.width, small.height), notes)
    log.info("cover: %s %dx%d", label, cover.width, cover.height)
    return cover, notes
