"""MusicBrainz lookup and the logic that narrows several releases down to one."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from urllib.parse import quote

from .config import log
from .discid import Toc
from .flacfile import FlacInfo
import re

from .util import NetworkError, NotFound, http_get, similar, strip_edition

VARIOUS = "89ad4ac3-39f7-470e-963a-56509c546377"
INC = "artist-credits+labels+recordings+release-groups+isrcs"


@dataclass
class Track:
    number: int
    title: str
    artist: str
    artists: list[str]
    artist_ids: list[str]
    recording_id: str
    track_id: str


@dataclass
class Album:
    mbid: str
    group_id: str
    title: str
    artist: str
    artist_sort: str
    artist_ids: list[str]
    date: str
    original_date: str
    country: str
    status: str
    release_type: str
    barcode: str
    label: str
    catalog: str
    media: str
    disc: int
    disc_total: int
    tracks: list[Track]
    genres: list[str] = field(default_factory=list)

    @property
    def various(self) -> bool:
        return VARIOUS in self.artist_ids


@dataclass
class Identification:
    album: Album | None = None
    method: str = "none"
    candidates: list[str] = field(default_factory=list)   # human-readable, for the report
    notes: list[str] = field(default_factory=list)
    offline: bool = False                                  # MusicBrainz could not be reached


class MusicBrainz:
    BASE = "https://musicbrainz.org/ws/2"

    def __init__(self, cfg: dict):
        self.ua = cfg["_user_agent"]
        self._last = 0.0

    def _get(self, path: str, **params: str) -> dict:
        wait = 1.1 - (time.monotonic() - self._last)     # MusicBrainz allows ~1 request/second
        if wait > 0:
            time.sleep(wait)
        params["fmt"] = "json"
        query = "&".join(f"{k}={quote(str(v), safe='+')}" for k, v in params.items() if v)
        try:
            return json.loads(http_get(f"{self.BASE}/{path}?{query}", self.ua))
        finally:
            self._last = time.monotonic()

    def by_disc(self, discid: str | None, toc: Toc | None, all_formats: bool = False) -> dict:
        # all_formats: let the fuzzy TOC match return non-CD media (Digital Media) too
        return self._get(f"discid/{discid or '-'}", inc=INC, toc=toc.toc_param if toc else "",
                         **({"media-format": "all"} if all_formats and toc else {}))

    def release(self, mbid: str) -> dict:
        return self._get(f"release/{mbid}", inc=INC + "+discids")

    def search(self, artist: str, album: str, ntracks: int) -> list[dict]:
        esc = lambda s: s.replace("\\", " ").replace('"', " ")
        hits: list[dict] = []
        # MusicBrainz titles carry no "(Deluxe Version)"-style suffix: search without it,
        # then without any bracketed part at all.
        for title in dict.fromkeys([strip_edition(album), strip_edition(album, all_brackets=True)]):
            q = f'release:"{esc(title)}" AND tracksmedium:{ntracks}'
            if artist:
                q += f' AND artist:"{esc(artist)}"'
            hits = self._get("release", query=q.replace("+", " "), limit="50").get("releases", [])
            if hits:
                break
        return hits

    def genres(self, group_id: str, limit: int) -> list[str]:
        data = self._get(f"release-group/{group_id}", inc="genres")
        ranked = sorted(data.get("genres", []), key=lambda g: (-g.get("count", 0), g.get("name", "")))
        return [g["name"].title() for g in ranked[:limit]]


# ---- choosing one release ----------------------------------------------------

def _format_tier(fmt: str, source: str) -> int:
    """Ripped discs want a CD release; existing FLAC files want Digital Media, then CD."""
    f = (fmt or "").lower()
    kind = "cd" if f == "cd" else "cdlike" if "cd" in f else "digital" if f == "digital media" \
        else "unknown" if not f else "other"       # cdlike: HDCD, Enhanced CD, SHM-CD, CD-R ...
    order = ["cd", "cdlike", "unknown", "digital", "other"] if source == "rip" \
        else ["digital", "cd", "cdlike", "unknown", "other"]
    return order.index(kind)


def _digits(code: str) -> str:
    return "".join(c for c in code or "" if c.isdigit()).lstrip("0")


def _completeness(rel: dict, medium: dict) -> int:
    """How much metadata a release carries - the tie-breaker between otherwise equal releases."""
    info = (rel.get("label-info") or [{}])[0]
    tracks = medium.get("tracks") or []
    return sum(map(bool, [
        rel.get("barcode"), rel.get("country"), rel.get("date"), len(rel.get("date") or "") >= 10,
        (info.get("label") or {}).get("name"), info.get("catalog-number"), rel.get("packaging"),
        rel.get("asin"), (rel.get("text-representation") or {}).get("language"),
        (rel.get("cover-art-archive") or {}).get("front"), medium.get("format"),
        tracks and all(t.get("length") for t in tracks),
    ]))


_CLEAN = re.compile(r"\b(clean|edited|censored)\b", re.I)
_EXPLICIT = re.compile(r"\bexplicit\b", re.I)


def _pick_medium(rel: dict, ntracks: int, discid: str | None, durations: list[float],
                 max_diff: float, trust_length: bool) -> tuple[dict | None, float]:
    """Find the medium of this release that is our disc. Returns (medium, worst track diff)."""
    best, best_diff = None, 0.0
    for m in rel.get("media", []):
        tracks = m.get("tracks") or []
        if len(tracks) != ntracks:
            continue
        diffs = [abs(t["length"] / 1000 - d) for t, d in zip(tracks, durations) if t.get("length")]
        worst = max(diffs, default=0.0)
        attached = bool(discid) and any(d.get("id") == discid for d in m.get("discs", []))
        if attached:
            return m, worst
        if worst > max_diff and not trust_length:
            continue
        if best is None or worst < best_diff:
            best, best_diff = m, worst
    return best, best_diff


def choose(releases: list[dict], *, ntracks: int, discid: str | None, durations: list[float],
           prefs: dict, exact: bool, source: str = "rip", barcode: str = "", asin: str = "",
           isrcs: list[str] = (), want_clean: bool = False) -> tuple[tuple[dict, dict] | None, list[str]]:
    """Rank candidate releases; lower key wins. Returns ((release, medium) | None, report lines).

    Order of importance:
      1. barcode or Amazon ASIN equal to one already tagged on the files
      2. most tracks whose ISRC tag matches the release's recordings
      3. Official before promo/bootleg
      4. not in avoid_countries
      5. explicit vs clean edition (explicit unless the files say Rating=Clean)
      6. format: CD first for rips; Digital Media, then CD, for existing FLAC files
      7. prefer_countries order, then everything else
      8. has a barcode at all
      9. has front cover art on MusicBrainz
     10. most complete metadata
     11. earliest date"""
    prefer = [c.upper() for c in prefs["prefer_countries"]]
    avoid = {c.upper() for c in prefs["avoid_countries"]}
    status_rank = {"official": 0, "": 1, "promotion": 2, "pseudo-release": 3, "bootleg": 4}
    want = _digits(barcode)
    ranked = []
    for rel in releases:
        medium, worst = _pick_medium(rel, ntracks, discid, durations,
                                     prefs["max_track_diff_seconds"], trust_length=exact)
        if medium is None:
            continue
        country = (rel.get("country") or "").upper()
        full = _completeness(rel, medium)
        hits = sum(1 for t, code in zip(medium.get("tracks") or [], isrcs)
                   if code and code in (t.get("recording") or {}).get("isrcs", []))
        note = rel.get("disambiguation") or ""
        wrong_rating = bool((_EXPLICIT if want_clean else _CLEAN).search(note)) if prefs["prefer_explicit"] or want_clean else False
        same_product = (want and _digits(rel.get("barcode")) == want) or (asin and rel.get("asin") == asin)
        key = (0 if same_product else 1,
               -hits,
               status_rank.get((rel.get("status") or "").lower(), 5),
               1 if country in avoid else 0,
               1 if wrong_rating else 0,
               _format_tier(medium.get("format", ""), source),
               prefer.index(country) if country in prefer else len(prefer),
               0 if rel.get("barcode") else 1,
               0 if (rel.get("cover-art-archive") or {}).get("front") else 1,
               -full,
               rel.get("date") or "9999",
               rel["id"])
        ranked.append((key, rel, medium, worst, full, hits))
    ranked.sort(key=lambda r: r[0])
    lines = []
    for i, (key, rel, medium, worst, full, hits) in enumerate(ranked):
        lines.append(f"{'->' if i == 0 else '  '} {rel.get('country') or '??':3} {rel.get('date') or '?':10} "
                     f"{medium.get('format') or '?':14} {rel.get('status') or '?':9} "
                     f"barcode={rel.get('barcode') or '-':14}{'*' if key[0] == 0 else ' '} "
                     f"isrc={hits:2} meta={full:2} maxdiff={worst:4.1f}s  {rel['id']}  {rel.get('title', '')}"
                     + (f" ({rel['disambiguation']})" if rel.get("disambiguation") else ""))
    skipped = len(releases) - len(ranked)
    if skipped:
        lines.append(f"   ({skipped} release(s) dropped: no medium with {ntracks} tracks of matching length)")
    return ((ranked[0][1], ranked[0][2]) if ranked else None), lines


def _credit(ac: list[dict]) -> tuple[str, list[str], list[str]]:
    text = "".join((c.get("name") or c["artist"]["name"]) + c.get("joinphrase", "") for c in ac)
    return text, [c["artist"]["name"] for c in ac], [c["artist"]["id"] for c in ac]


def build_album(rel: dict, medium: dict) -> Album:
    artist, _, artist_ids = _credit(rel.get("artist-credit", []))
    rg = rel.get("release-group") or {}
    info = (rel.get("label-info") or [{}])[0]
    tracks = []
    for i, t in enumerate(medium["tracks"], 1):
        rec = t.get("recording") or {}
        ac = t.get("artist-credit") or rec.get("artist-credit") or rel.get("artist-credit", [])
        name, names, ids = _credit(ac)
        tracks.append(Track(i, t.get("title") or rec.get("title", f"Track {i:02d}"), name, names, ids,
                            rec.get("id", ""), t.get("id", "")))
    sort = "; ".join(c["artist"].get("sort-name", c["artist"]["name"]) for c in rel.get("artist-credit", []))
    types = [rg.get("primary-type") or "", *(rg.get("secondary-types") or [])]
    return Album(
        mbid=rel["id"], group_id=rg.get("id", ""), title=rel.get("title", ""), artist=artist,
        artist_sort=sort, artist_ids=artist_ids, date=rel.get("date") or "",
        original_date=rg.get("first-release-date") or "", country=rel.get("country") or "",
        status=(rel.get("status") or "").lower(), release_type="; ".join(t.lower() for t in types if t),
        barcode=rel.get("barcode") or "", label=(info.get("label") or {}).get("name", ""),
        catalog=info.get("catalog-number") or "", media=medium.get("format") or "",
        disc=medium.get("position", 1), disc_total=len(rel.get("media", [])) or 1, tracks=tracks)


# ---- the identification ladder -----------------------------------------------

def identify(mb: MusicBrainz, cfg: dict, infos: list[FlacInfo], *, discid: str | None,
             toc: Toc | None, forced_release: str | None = None, source: str = "rip") -> Identification:
    """Find the release. source="rip": the disc ID decides, text search is only a fallback.
    source="files": disc ID / TOC hits and a text search on the existing tags are pooled, so a
    Digital Media release can win over the CD pressing the track lengths happen to match."""
    prefs = cfg["musicbrainz"]
    out = Identification()
    ntracks = len(infos)
    durations = [i.seconds for i in infos]
    barcodes = {i.tag("BARCODE", "UPC") for i in infos}
    barcode = barcodes.pop() if len(barcodes) == 1 else ""
    asins = {m.group(0) for i in infos for key in ("ASIN", "WWW", "URL")
             for m in [re.search(r"\bB0[0-9A-Z]{8}\b", i.tag(key))] if m}
    asin = asins.pop() if len(asins) == 1 else ""
    want_clean = any(i.tag("RATING", "ITUNESADVISORY").lower() in ("clean", "2") for i in infos)
    pick = lambda rels, exact, did: choose(
        rels, ntracks=ntracks, discid=did, durations=durations, prefs=prefs, exact=exact, source=source,
        barcode=barcode, asin=asin,
        isrcs=[i.tag("ISRC").upper().replace("-", "") for i in infos], want_clean=want_clean)

    def accept(found, lines, method) -> bool:
        out.candidates = lines or out.candidates
        if not found:
            return False
        out.album, out.method = build_album(*found), method
        try:
            if out.album.group_id and prefs["genres"]:
                out.album.genres = mb.genres(out.album.group_id, prefs["genres"])
        except (NotFound, NetworkError) as e:
            out.notes.append(f"genre lookup failed: {e}")
        return True

    hint_artist = hint_album = ""
    try:
        tagged = {i.tag("MUSICBRAINZ_ALBUMID") for i in infos}
        tagged_id = tagged.pop() if len(tagged) == 1 else ""
        for mbid, method, exact in ((forced_release, "forced release id", True),
                                    (tagged_id, "MUSICBRAINZ_ALBUMID tag", False)):
            if not mbid:
                continue
            try:
                if accept(*pick([mb.release(mbid)], exact, discid), method):
                    return out
                out.notes.append(f"{method} {mbid}: track count/lengths do not match these files")
            except NotFound:
                out.notes.append(f"{method} {mbid}: not found on MusicBrainz")

        pool: dict[str, dict] = {}
        attached = None                      # disc id, if MusicBrainz knows this exact disc
        lookup_id = discid or (toc.discid if toc and toc.exact else None)
        if lookup_id or toc:
            try:
                data = mb.by_disc(lookup_id, toc, all_formats=source != "rip")
            except NotFound:
                data = {}
            if "releases" in data:
                attached = lookup_id if lookup_id and data.get("id") == lookup_id else None
                pool.update((r["id"], r) for r in data["releases"])
                out.notes.append(f"disc lookup: {len(data['releases'])} release(s) by "
                                 + ("disc ID" if attached else "fuzzy TOC (track lengths)"))
            elif data.get("title"):          # a "CD stub": user-submitted title/artist only
                hint_artist, hint_album = data.get("artist", ""), data["title"]
                out.notes.append(f"disc ID matches only a CD stub: {hint_artist} / {hint_album}")
        if source == "rip" and pool and accept(*pick(list(pool.values()), bool(attached), attached),
                                               "disc ID" if attached else "fuzzy TOC (track lengths)"):
            return out

        artists = {i.tag("ALBUMARTIST", "ALBUM ARTIST", "ARTIST") for i in infos}
        albums = {i.tag("ALBUM") for i in infos}
        artist = hint_artist or (artists.pop() if len(artists) == 1 else "")
        album = hint_album or (albums.pop() if len(albums) == 1 else "")
        if album and artist != cfg["naming"]["unknown_artist"]:
            hits = [h for h in mb.search(artist, album, ntracks)
                    if similar(h.get("title", ""), album) >= 0.85 and h["id"] not in pool][:15]
            full = [mb.release(h["id"]) for h in hits]
            if artist:
                full = [r for r in full if similar(_credit(r.get("artist-credit", []))[0], artist) >= 0.8]
            pool.update((r["id"], r) for r in full)
            out.notes.append(f"tag search ({artist or '?'} / {album}): {len(full)} more release(s)")
        if pool and accept(*pick(list(pool.values()), False, attached),
                           "disc/TOC lookup + tag search, ranked together"):
            return out
    except NetworkError as e:
        out.offline = True
        out.notes.append(f"MusicBrainz unreachable: {e}")
        log.warning("MusicBrainz unreachable: %s", e)
    return out
