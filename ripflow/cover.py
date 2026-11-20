"""Cover art: find a large, confidently-matching front cover; make cover.jpg + a small embed copy."""
from __future__ import annotations

import io
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from .config import log
from .flacfile import FlacError, read_picture
from .util import NetworkError, NotFound, http_get, norm, similar, strip_edition


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


def _base_title(title: str) -> str:
    """The title without its edition parts: bracketed text and anything after " - "."""
    base = re.sub(r"\s[-\u2013\u2014]\s.*$", "", re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", title or "")).strip()
    return base or (title or "")


def _matches(artist: str, album: str, got_artist: str, got_album: str, various: bool) -> float:
    """Name match on the base titles; edition parts are weighed separately (see _rank)."""
    a = 1.0 if various or not artist else similar(artist, got_artist)
    b = similar(_base_title(album), _base_title(got_album))
    return (a + b) / 2 if a >= 0.8 and b >= 0.85 else 0.0


# Stores list every edition of an album under nearly the same name ("X", "X (Deluxe)",
# "X (10th Anniversary Edition)", "X [Bonus Track Version]"), and MusicBrainz names editions
# however the person who entered them chose to. So a candidate is ranked on signals that do
# not depend on naming, strongest first:
#   1. its UPC equals the release's barcode (looked up directly - the exact product)
#   2. its track count equals ours
#   3. its edition text equals ours: whatever stands in brackets or after " - " in the title,
#      plus MusicBrainz's edition note, compared as a set of words (any name counts)
#   4. how closely the names match
#   5. explicit over clean (the cover is normally the same; the album id matters for animation)
_GENERIC = {"edition", "version", "explicit", "clean", "cleaned", "album", "the", "a", "and", "of",
            "original", "digital", "release"}
_EXTRA = re.compile(r"[\(\[]([^\)\]]*)[\)\]]|\s[-\u2013\u2014]\s(.+)$")


def edition_words(title: str, note: str = "") -> frozenset[str]:
    """The words that set an edition apart: the bracketed / dashed parts of a title plus an
    edition note, without generic words such as "edition", "version", "explicit"."""
    parts = [m.group(1) or m.group(2) or "" for m in _EXTRA.finditer(title or "")] + [note or ""]
    words = re.findall(r"[a-z0-9]+", " ".join(parts).lower())
    return frozenset(w for w in words if w not in _GENERIC)


def _rank(artist: str, album: str, ntracks: int, various: bool, edition: frozenset, got: dict,
          upc_hit: bool = False) -> tuple | None:
    """Sort key for a store result (higher is better), or None if it is not this album."""
    name = _matches(artist, album, got["artist"], got["album"], various)
    if upc_hit:                       # found by barcode: only the artist has to agree
        name = name or (1.0 if various or not artist or similar(artist, got["artist"]) >= 0.8 else 0.0)
    if not name:
        return None
    tracks = 1 if not ntracks or not got.get("tracks") else (2 if got["tracks"] == ntracks else 0)
    same_edition = 1 if edition_words(got["album"]) == edition else 0
    return (1 if upc_hit else 0, tracks, same_edition, round(name, 3), 0 if got.get("cleaned") else 1)


def _best(hits: list[tuple]) -> list[tuple]:
    """The best-ranked album: its images are used, never the largest image of any edition.
    Albums that tie on everything but explicit/clean count as one (same cover)."""
    if not hits:
        return []
    top = max(h[0] for h in hits)
    return sorted((h for h in hits if h[0][:4] == top[:4]), key=lambda h: h[0], reverse=True)


_artist_albums: dict = {}
_last_itunes = 0.0
stats = {"itunes": 0}           # iTunes search calls made this run (shown by `covers`)


def _itunes_get(url: str, ua: str, interval: float) -> bytes:
    """Apple allows roughly 20 search calls a minute; keep them `interval` seconds apart."""
    global _last_itunes
    wait = interval - (time.monotonic() - _last_itunes)
    if wait > 0:
        time.sleep(wait)
    try:
        stats["itunes"] += 1
        return http_get(url, ua)
    finally:
        _last_itunes = time.monotonic()


def is_animated(path: Path) -> bool:
    try:
        from PIL import Image
        with Image.open(path) as im:
            return bool(getattr(im, "is_animated", False))
    except Exception:
        return False


LEGACY_STILL_STEMS = ("cover orig",)     # earlier name of the still beside an animated cover


def migrate_legacy_names(folder: Path, cfg: dict, dry_run: bool = False) -> list[str]:
    """Rename stills saved under an earlier name ("cover orig.jpg/.webp") to the current one
    ("cover-static.jpg/.webp"). An identical copy under the old name is removed; a different
    one is left alone and reported."""
    new_stem = Path(cfg["cover"]["animated_still_name"]).stem
    done = []
    for stem in LEGACY_STILL_STEMS:
        if stem.lower() == new_stem.lower():
            continue
        for ext in (".jpg", ".webp"):
            old, new = folder / (stem + ext), folder / (new_stem + ext)
            if not old.is_file():
                continue
            verb = "would be " if dry_run else ""
            if not new.is_file():
                if not dry_run:
                    old.rename(new)
                done.append(f"{old.name} {verb}renamed to {new.name}")
            elif old.read_bytes() == new.read_bytes():
                if not dry_run:
                    old.unlink()
                done.append(f"{old.name} {verb}removed (same as {new.name})")
            else:
                done.append(f"{old.name} left alone ({new.name} exists and differs)")
    return done


def park_static_webp(folder: Path, cfg: dict, dry_run: bool = False) -> str | None:
    """A still (non-animated) cover.webp would compete with cover.jpg for the album image, and
    keeps the animated-cover search from running. Rename it to the alternate still name with a
    .webp extension ("cover-static.webp"); it stays available as a still-cover source.
    Returns what was (or, with dry_run, would be) done, or None if there was nothing to do."""
    webp = folder / "cover.webp"
    if not webp.is_file() or is_animated(webp):
        return None
    parked = folder / (Path(cfg["cover"]["animated_still_name"]).stem + ".webp")
    if parked.is_file():
        if parked.read_bytes() != webp.read_bytes():
            return f"cover.webp (not animated) left alone: {parked.name} exists and differs"
        if not dry_run:
            webp.unlink()
        return f"cover.webp (not animated) {'would be ' if dry_run else ''}removed (same as {parked.name})"
    if not dry_run:
        webp.rename(parked)
    return f"cover.webp (not animated) {'would be ' if dry_run else ''}renamed to {parked.name}"


def settle_still(folder: Path, cfg: dict, jpeg: bytes | None = None) -> str:
    """Put the still cover under the right name and say what was done.

    Servers show one album image. With an animated cover.webp the still goes under
    `animated_still_name` (default "cover-static.jpg"), which they do not pick up, so they show
    the animation; otherwise it is cover.jpg. `jpeg` given: write it. Otherwise just move an
    existing still to the right name. The copy under the other name is removed when it is
    redundant (just replaced, or byte-identical); two different files are both left alone."""
    names = ["cover.jpg", cfg["cover"]["animated_still_name"]]
    if is_animated(folder / "cover.webp"):
        names.reverse()
    right, wrong = folder / names[0], folder / names[1]
    if jpeg is not None:
        right.write_bytes(jpeg)
        if wrong.is_file():
            wrong.unlink()
        return f"saved as {right.name}"
    if not wrong.is_file():
        return f"{right.name} kept" if right.is_file() else "none"
    if not right.is_file():
        wrong.rename(right)
        return f"{wrong.name} renamed to {right.name}"
    if wrong.read_bytes() == right.read_bytes():
        wrong.unlink()
        return f"{right.name} kept, duplicate {wrong.name} removed"
    return f"both {right.name} and {wrong.name} exist and differ - left alone"


def _upc_candidates(barcode: str) -> list[str]:
    digits = "".join(c for c in barcode or "" if c.isdigit()).lstrip("0")
    if len(digits) < 8:
        return []
    return list(dict.fromkeys([digits.zfill(12) if len(digits) <= 12 else digits, digits.zfill(13)]))


def _itunes(cfg, ua, artist, album, various, ntracks, notes, found=None, barcode="", edition_note=""):
    """Apple's public catalogue search (the same artwork Apple Music shows).
    Tries: the barcode (exact product), the artist's full album list, artist + album search,
    then album-title search."""
    base = "https://itunes.apple.com"
    common = f"&media=music&country={cfg['itunes_country']}"
    get = lambda url: json.loads(_itunes_get(url, ua, cfg["itunes_interval"])).get("results", [])
    bare = strip_edition(album, True)

    def artist_albums():
        key = (norm(artist), cfg["itunes_country"])
        if key not in _artist_albums:          # one lookup per artist per run (a library has many per artist)
            found = get(f"{base}/search?term={quote(artist, safe='')}&entity=musicArtist&attribute=artistTerm&limit=10{common}")
            ids = [r["artistId"] for r in found if r.get("artistId") and similar(artist, r.get("artistName", "")) >= 0.9]
            _artist_albums[key] = [r for i in ids[:2] for r in get(f"{base}/lookup?id={i}&entity=album&limit=200{common}")
                                   if r.get("wrapperType") == "collection"]
        return _artist_albums[key]

    # The artist's own album list is the most reliable route (Apple's text search often misses),
    # and it is cached, so it goes first.
    def by_barcode():
        for upc in _upc_candidates(barcode):
            results = [r for r in get(f"{base}/lookup?upc={upc}&entity=album{common}")
                       if r.get("wrapperType") == "collection"]
            if results:
                return results
        return []

    edition = edition_words(album, edition_note)
    queries = ([("barcode", by_barcode)] if _upc_candidates(barcode) else []) + \
              ([] if various or not artist else [("artist's album list", artist_albums)]) + [
        ("artist + album", lambda: get(f"{base}/search?term={quote(album if various else f'{artist} {album}', safe='')}&entity=album&limit=50{common}")),
        ("album title", lambda: get(f"{base}/search?term={quote(bare, safe='')}&entity=album&attribute=albumTerm&limit=50{common}")),
    ]
    hits = []
    for name, run in queries:
        results = run()
        for r in results:
            score = _rank(artist, album, ntracks, various, edition,
                          {"artist": r.get("artistName", ""), "album": r.get("collectionName", ""),
                           "tracks": r.get("trackCount"), "cleaned": r.get("collectionExplicitness") == "cleaned"},
                          upc_hit=name == "barcode")
            art = r.get("artworkUrl100")
            if score and art:
                big = re.sub(r"/\d+x\d+[a-z]*(-\d+)?\.(jpg|png)$", f"/{cfg['max_size']}x{cfg['max_size']}bb.jpg", art)
                hits.append((score, big, r.get("collectionId"), r.get("collectionName", ""), r.get("trackCount")))
        notes.append(f"iTunes/Apple Music, {name}: {len(results)} result(s), {len(hits)} matching")
        if hits:
            break
        if results:      # show what came back, so a matching problem can be diagnosed from the report
            notes.append("    e.g. " + "; ".join(f"{r.get('artistName')} / {r.get('collectionName')}" for r in results[:4]))
    best = _best(hits)
    if best:
        others = sorted({f"{h[3]} ({h[4]} tracks)" for h in hits if h not in best})
        notes.append(f"    using: {best[0][3]} ({best[0][4]} tracks{', matched by barcode' if best[0][0][0] else ''})"
                     + (f"; not used: {'; '.join(others)}" if others else ""))
    if found is not None and best and best[0][2]:
        found["apple_album_id"] = str(best[0][2])
    return list(dict.fromkeys(h[1] for h in best))[:2]


def apple_album_id(cfg: dict, *, artist: str, album: str, various: bool = False, ntracks: int = 0,
                   notes: list[str] | None = None, barcode: str = "", edition_note: str = "") -> str | None:
    """Apple Music id of the album (same matching as the cover search), or None."""
    found: dict = {}
    try:
        _itunes(cfg["cover"], cfg["_user_agent"], artist, album, various, ntracks,
                notes if notes is not None else [], found, barcode, edition_note)
    except (NetworkError, NotFound, ValueError) as e:
        if notes is not None:
            notes.append(f"iTunes/Apple Music: {e}")
    return found.get("apple_album_id")


def _deezer(cfg, ua, artist, album, various, ntracks, notes, edition_note=""):
    q = quote(f'album:"{album}"' if various else f'artist:"{artist}" album:"{album}"')
    hits = []
    data = json.loads(http_get(f"https://api.deezer.com/search/album?q={q}&limit=25", ua)).get("data", [])
    edition = edition_words(album, edition_note)
    for r in data:
        score = _rank(artist, album, ntracks, various, edition,
                      {"artist": (r.get("artist") or {}).get("name", ""), "album": r.get("title", ""),
                       "tracks": r.get("nb_tracks")})
        if score and r.get("cover_xl"):
            hits.append((score, r["cover_xl"]))
    notes.append(f"Deezer search: {len(data)} result(s), {len(hits)} matching artist + album")
    return [h[1] for h in _best(hits)][:2]


def find_cover(cfg: dict, *, artist: str, album: str, various: bool = False, ntracks: int = 0,
               mbid: str = "", group_id: str = "", local_dirs: list[Path] = (),
               embedded_from: Path | None = None, only_local: bool = False,
               found: dict | None = None, barcode: str = "", edition_note: str = "") -> tuple[Cover | None, list[str]]:
    """Largest usable image from `sources`; only if those yield nothing, the largest from
    `fallback_sources`. Slightly non-square images are centre-cropped to a square. Never raises.

    only_local: use nothing but cover/folder/front image files in `local_dirs` (no embedded
    art, no online lookups). If there are several, the most recently modified one wins, so a
    file you just dropped in overrides older art.

    An animated image (e.g. an animated cover.webp) is only used as the still cover when
    nothing else qualifies; its first frame is used then. `found` (a dict), if given,
    receives "apple_album_id" when the iTunes search matched the album."""
    c, ua, notes = cfg["cover"], cfg["_user_agent"], []
    try:
        from PIL import Image
    except ImportError:
        return None, ["Pillow is not installed (pip install pillow) - cover art skipped"]

    best: tuple[float, bytes, str] | None = None
    animated_fallback: tuple[bytes, str] | None = None

    def consider(data: bytes, label: str, mtime: float | None) -> bool:
        nonlocal best, animated_fallback
        try:
            with Image.open(io.BytesIO(data)) as im:
                w, h = im.size
                moving = getattr(im, "is_animated", False)
        except Exception as e:
            notes.append(f"{label}: unreadable image ({e})")
            return False
        if moving:
            notes.append(f"{label}: {w}x{h} animated - not used as the still cover")
            if min(w, h) >= c["min_size"] and animated_fallback is None:
                animated_fallback = (data, label)
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
                            if f.stem.lower() in ("cover", "folder", "front", Path(c["animated_still_name"]).stem.lower(),
                                                  *LEGACY_STILL_STEMS) and \
                                    f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                                yield f"local file {f.name}", f.read_bytes(), f.stat().st_mtime
                    art = read_picture(embedded_from) if embedded_from and not only_local else None
                    if art:
                        yield f"embedded in {Path(embedded_from).name}", art, None
                elif name == "itunes" and album:
                    for u in _itunes(c, ua, artist, album, various, ntracks, notes, found, barcode, edition_note):
                        yield "iTunes/Apple Music", http_get(u, ua), None
                elif name == "deezer" and album:
                    for u in _deezer(c, ua, artist, album, various, ntracks, notes, edition_note):
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
    if best is None and animated_fallback:
        data, label = animated_fallback
        best = (0, data, label)
        notes.append(f"nothing else usable: first frame of {label} used as the still cover")
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
