"""Cover art: find a large, confidently-matching front cover; make cover.jpg + a small embed copy."""
from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from .config import log
from .flacfile import FlacError, read_picture
from .util import NetworkError, NotFound, http_get, similar, strip_edition


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


def _itunes(cfg, ua, artist, album, various, ntracks, notes):
    """Apple's public catalogue search (the same artwork Apple Music shows).
    Tries: artist + album search, album-title search, then the artist's full album list."""
    base = "https://itunes.apple.com"
    common = f"&media=music&country={cfg['itunes_country']}"
    get = lambda url: json.loads(http_get(url, ua)).get("results", [])
    bare = strip_edition(album, True)

    def artist_albums():
        found = get(f"{base}/search?term={quote(artist, safe='')}&entity=musicArtist&attribute=artistTerm&limit=10{common}")
        ids = [r["artistId"] for r in found if r.get("artistId") and similar(artist, r.get("artistName", "")) >= 0.9]
        return [r for i in ids[:2] for r in get(f"{base}/lookup?id={i}&entity=album&limit=200{common}")
                if r.get("wrapperType") == "collection"]

    queries = [
        ("artist + album", lambda: get(f"{base}/search?term={quote(album if various else f'{artist} {album}', safe='')}&entity=album&limit=50{common}")),
        ("album title", lambda: get(f"{base}/search?term={quote(bare, safe='')}&entity=album&attribute=albumTerm&limit=50{common}")),
    ] + ([] if various or not artist else [("artist's album list", artist_albums)])
    hits = []
    for name, run in queries:
        results = run()
        for r in results:
            score = _matches(artist, album, r.get("artistName", ""), r.get("collectionName", ""), various)
            art = r.get("artworkUrl100")
            if score and art:
                score += 0.1 if r.get("trackCount") == ntracks else 0
                big = re.sub(r"/\d+x\d+[a-z]*(-\d+)?\.(jpg|png)$", f"/{cfg['max_size']}x{cfg['max_size']}bb.jpg", art)
                hits.append((score, big))
        notes.append(f"iTunes/Apple Music, {name}: {len(results)} result(s), {len(hits)} matching")
        if hits:
            break
        if results:      # show what came back, so a matching problem can be diagnosed from the report
            notes.append("    e.g. " + "; ".join(f"{r.get('artistName')} / {r.get('collectionName')}" for r in results[:4]))
    return list(dict.fromkeys(u for _, u in sorted(hits, reverse=True)))[:3]


def _deezer(cfg, ua, artist, album, various, ntracks, notes):
    q = quote(f'album:"{album}"' if various else f'artist:"{artist}" album:"{album}"')
    hits = []
    data = json.loads(http_get(f"https://api.deezer.com/search/album?q={q}&limit=25", ua)).get("data", [])
    for r in data:
        score = _matches(artist, album, (r.get("artist") or {}).get("name", ""), r.get("title", ""), various)
        if score and r.get("cover_xl"):
            hits.append((score + (0.1 if r.get("nb_tracks") == ntracks else 0), r["cover_xl"]))
    notes.append(f"Deezer search: {len(data)} result(s), {len(hits)} matching artist + album")
    return [u for _, u in sorted(hits, reverse=True)[:2]]


def find_cover(cfg: dict, *, artist: str, album: str, various: bool = False, ntracks: int = 0,
               mbid: str = "", group_id: str = "", local_dirs: list[Path] = (),
               embedded_from: Path | None = None, only_local: bool = False) -> tuple[Cover | None, list[str]]:
    """Largest usable image from `sources`; only if those yield nothing, the largest from
    `fallback_sources`. Slightly non-square images are centre-cropped to a square. Never raises.

    only_local: use nothing but cover/folder/front image files in `local_dirs` (no embedded
    art, no online lookups). If there are several, the most recently modified one wins, so a
    file you just dropped in overrides older art."""
    c, ua, notes = cfg["cover"], cfg["_user_agent"], []
    try:
        from PIL import Image
    except ImportError:
        return None, ["Pillow is not installed (pip install pillow) - cover art skipped"]

    best: tuple[float, bytes, str] | None = None

    def consider(data: bytes, label: str, mtime: float | None) -> bool:
        nonlocal best
        try:
            with Image.open(io.BytesIO(data)) as im:
                w, h = im.size
        except Exception as e:
            notes.append(f"{label}: unreadable image ({e})")
            return False
        if max(w, h) > c["max_aspect"] * min(w, h):
            notes.append(f"{label}: {w}x{h} is too far from square (probably not a plain front cover), skipped")
            return False
        notes.append(f"{label}: {w}x{h}" + ("" if w == h else f" (will be cropped to {min(w, h)}x{min(w, h)})"))
        score = mtime if only_local else min(w, h)      # newest file / largest image wins
        if min(w, h) >= c["min_size"] and (best is None or score > best[0]):
            best = (score, data, label)
        return not only_local and min(w, h) >= c["max_size"]

    def sources(names):
        for name in names:
            try:
                if name == "local":
                    for d in dict.fromkeys(Path(d).resolve() for d in local_dirs):
                        for f in sorted(Path(d).glob("*")):
                            if f.stem.lower() in ("cover", "folder", "front") and \
                                    f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                                yield f"local file {f.name}", f.read_bytes(), f.stat().st_mtime
                    art = read_picture(embedded_from) if embedded_from and not only_local else None
                    if art:
                        yield f"embedded in {Path(embedded_from).name}", art, None
                elif name == "itunes" and album:
                    for u in _itunes(c, ua, artist, album, various, ntracks, notes):
                        yield "iTunes/Apple Music", http_get(u, ua), None
                elif name == "deezer" and album:
                    for u in _deezer(c, ua, artist, album, various, ntracks, notes):
                        yield "Deezer", http_get(u, ua), None
                elif name == "coverartarchive":
                    for kind, ident in (("release", mbid), ("release-group", group_id)):
                        if ident:
                            try:
                                yield (f"Cover Art Archive ({kind})",
                                       http_get(f"https://coverartarchive.org/{kind}/{ident}/front", ua, timeout=60),
                                       None)
                            except NotFound:
                                notes.append(f"Cover Art Archive ({kind}): no front image")
            except (NetworkError, NotFound, FlacError, OSError, ValueError) as e:
                notes.append(f"{name}: {e}")

    groups = (["local"],) if only_local else (c["sources"], c["fallback_sources"])
    for group in groups:
        for label, data, mtime in sources(group):
            if consider(data, label, mtime):
                break
        if best is not None:
            break
    if best is None:
        return None, notes

    _, data, label = best
    if only_local:
        notes.append(f"newest local image used: {label}")
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        original_jpeg = im.format == "JPEG" and im.mode == "RGB" and im.width == im.height
        rgb = im.convert("RGB")
    if rgb.width != rgb.height:             # centre-crop (never stretch) to a square
        side = min(rgb.size)
        left, top = (rgb.width - side) // 2, (rgb.height - side) // 2
        rgb = rgb.crop((left, top, left + side, top + side))
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
