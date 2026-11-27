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

from .util import NetworkError, NotFound, ascii_punctuation, http_get, similar, strip_edition

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
    artist_sort: str = ""
    isrcs: list[str] = field(default_factory=list)


@dataclass
class Album:
    mbid: str
    group_id: str
    title: str
    artist: str
    artist_sort: str
    artist_ids: list[str]
    artists: list[str]            # the individual credited album artists
    date: str
    original_date: str
    country: str
    status: str
    release_type: str
    barcode: str
    labels: list[str]
    catalogs: list[str]
    asin: str
    script: str
    media: str
    disc: int
    disc_total: int
    tracks: list[Track]
    genres: list[str] = field(default_factory=list)
    disambiguation: str = ""      # MusicBrainz's edition note, e.g. "deluxe edition, explicit"

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
    skipped: bool = False                                  # the user chose to leave the album alone


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
    """Ripped discs want a CD release; existing FLAC files want Digital Media, then CD.
    Enhanced CD, HDCD, SHM-CD, CD-R and the like count as CD."""
    f = (fmt or "").lower()
    kind = "cd" if "cd" in f else "digital" if f == "digital media" else "unknown" if not f else "other"
    order = ["cd", "unknown", "digital", "other"] if source == "rip" else ["digital", "cd", "unknown", "other"]
    return order.index(kind)


def _digits(code: str) -> str:
    return "".join(c for c in code or "" if c.isdigit()).lstrip("0")


def release_countries(rel: dict) -> list[str]:
    """Every region a release came out in: its release events, in order, plus `country`."""
    codes = [rel.get("country") or ""]
    for event in rel.get("release-events") or []:
        codes += (event.get("area") or {}).get("iso-3166-1-codes") or []
    return list(dict.fromkeys(c.upper() for c in codes if c))


def release_country(rel: dict, prefs: dict) -> str:
    """The region to use for a release with several: the first of prefer_countries that it has
    (US, then XW, then XE by default), else the first not in avoid_countries, else the first."""
    codes = release_countries(rel)
    avoid = {c.upper() for c in prefs.get("avoid_countries", [])}
    for c in (x.upper() for x in prefs.get("prefer_countries", [])):
        if c in codes:
            return c
    return next((c for c in codes if c not in avoid), codes[0] if codes else "")


def _completeness(rel: dict, medium: dict) -> int:
    """How much metadata a release carries - the tie-breaker between otherwise equal releases."""
    info = (rel.get("label-info") or [{}])[0]
    tracks = medium.get("tracks") or []
    return sum(map(bool, [
        rel.get("barcode"), release_countries(rel), rel.get("date"), len(rel.get("date") or "") >= 10,
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
        # Matching on length needs lengths: a release whose tracks mostly have none on
        # MusicBrainz proves nothing (it used to pass as a perfect fit).
        if not trust_length and (worst > max_diff or len(diffs) < max(1, (ntracks + 1) // 2)):
            continue
        if best is None or worst < best_diff:
            best, best_diff = m, worst
    return best, best_diff


@dataclass
class Candidate:
    key: tuple
    release: dict
    medium: dict
    worst: float          # largest track-length difference, seconds
    meta: int             # metadata completeness score
    isrc_hits: int
    country: str = ""     # the region chosen for this release (see release_country)


def rank(releases: list[dict], *, ntracks: int, discid: str | None, durations: list[float],
         prefs: dict, exact: bool, source: str = "rip", barcode: str = "", asin: str = "",
         isrcs: list[str] = (), want_clean: bool = False, ndiscs: int | None = None) -> list[Candidate]:
    """Rank candidate releases, best first (lowest key). Releases with no medium matching
    these files are left out.

    Order of importance:
      1. barcode or Amazon ASIN equal to one already tagged on the files
      2. most tracks whose ISRC tag matches the release's recordings
      3. Official before promo/bootleg
      4. not in avoid_countries
      5. explicit vs clean edition (explicit unless the files say Rating=Clean)
         then: as many discs as the album folder holds (existing files only)
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
        country = release_country(rel, prefs)
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
               0 if not ndiscs or len(rel.get("media") or []) == ndiscs else 1,
               _format_tier(medium.get("format", ""), source),
               prefer.index(country) if country in prefer else len(prefer),
               0 if rel.get("barcode") else 1,
               0 if (rel.get("cover-art-archive") or {}).get("front") else 1,
               -full,
               rel.get("date") or "9999",
               rel["id"])
        ranked.append(Candidate(key, rel, medium, worst, full, hits, country))
    ranked.sort(key=lambda c: c.key)
    return ranked


def _media(rel: dict, medium: dict) -> str:
    """The format, with the disc count when there are several: "CD", "3xCD"."""
    n = len(rel.get("media") or [])
    return (f"{n}x" if n > 1 else "") + (medium.get("format") or "?")


def tier_size(ranked: list[Candidate]) -> int:
    """How many candidates match the files as well as the top one does: same barcode/ASIN
    result, same ISRC matches, same release status and same explicit/clean fit. They differ
    only in preferences (format, country, completeness...), so any of them could be the
    right one - that is when it is worth asking."""
    if not ranked:
        return 0
    head = lambda c: (c.key[0], c.key[1], c.key[2], c.key[4])
    return sum(1 for c in ranked if head(c) == head(ranked[0]))


def report_lines(ranked: list[Candidate], *, total: int, ntracks: int, chosen: str | None = None) -> list[str]:
    """One line per candidate for the report; `->` marks the release used (default: the top one)."""
    chosen = chosen or (ranked[0].release["id"] if ranked else None)
    lines = []
    for c in ranked:
        rel = c.release
        lines.append(f"{'->' if rel['id'] == chosen else '  '} {c.country or '??':3} {rel.get('date') or '?':10} "
                     f"{_media(rel, c.medium):14} {rel.get('status') or '?':9} "
                     f"barcode={rel.get('barcode') or '-':14}{'*' if c.key[0] == 0 else ' '} "
                     f"isrc={c.isrc_hits:2} meta={c.meta:2} maxdiff={c.worst:4.1f}s  {rel['id']}  {rel.get('title', '')}"
                     + (f" ({rel['disambiguation']})" if rel.get("disambiguation") else ""))
    if total - len(ranked) > 0:
        lines.append(f"   ({total - len(ranked)} release(s) dropped: no medium with {ntracks} tracks of matching length)")
    return lines


def choose(releases: list[dict], **kw) -> tuple[tuple[dict, dict] | None, list[str]]:
    """Best release and the report lines (non-interactive). Same arguments as rank()."""
    ranked = rank(releases, **kw)
    lines = report_lines(ranked, total=len(releases), ntracks=kw["ntracks"])
    return ((ranked[0].release, ranked[0].medium) if ranked else None), lines


def _credit(ac: list[dict]) -> tuple[str, list[str], list[str]]:
    text = "".join((c.get("name") or c["artist"]["name"]) + c.get("joinphrase", "") for c in ac)
    return text, [c["artist"]["name"] for c in ac], [c["artist"]["id"] for c in ac]


def title_case(text: str) -> str:
    """Capitalise each word that is all lower case ("red album" -> "Red Album"), leaving words
    that already have a capital alone ("US", "2xLP", "McCartney"). Words also start after a
    bracket, slash or hyphen; an apostrophe does not start one ("don't" stays "Don't")."""
    return re.sub(r"(?<![\w'’])([a-z])(\w*)",
                  lambda m: m.group(0) if not m.group(0).islower() else m.group(1).upper() + m.group(2), text)


def _comment_key(text: str) -> str:
    """For comparing comment texts: lower case, brackets and punctuation around it removed,
    spaces and hyphens squeezed ("[E]" -> "e", "Explicit-Lyrics" -> "explicit lyrics")."""
    return " ".join(re.sub(r"[\s\-_]+", " ", text.lower()).strip(" ()[]{}<>.:;!?'\"").split())


_FORMAT_MARK = re.compile(r"\d{1,2}bits?|bits?|\d{2,3}(?:[.,]\d+)?k(?:hz)?|khz|hi|res|hires|mqa|sacd|dsd\d*", re.I)
_FORMAT_FILL = re.compile(r"\d{1,3}(?:[.,]\d+)?|audio|and|flac|pcm|lossless|quality", re.I)


def is_audio_format(text: str) -> bool:
    """A comment part that only states the audio format: "24-bit 48kHz", "24bit/96kHz",
    "16-Bit / 44.1 kHz", "24/96", "Hi-Res Audio", "MQA". Every word must be a format word, with
    at least one real marker (bit, kHz, hi-res, MQA...) - so "1999" or "Volume 2" stay - except
    the bare "24/96" form."""
    t = text.strip(" ()[]{}").lower()
    if re.fullmatch(r"\d{2}\s*/\s*\d{2,3}(?:[.,]\d+)?", t):
        return True
    words = [w for w in re.split(r"[\s/\-_+&|]+", t) if w]
    return bool(words) and all(_FORMAT_MARK.fullmatch(w) or _FORMAT_FILL.fullmatch(w) for w in words) \
        and any(_FORMAT_MARK.fullmatch(w) for w in words)


def comment_for_title(rel: dict, skip: list[str], formats: bool = True) -> str:
    """The release's disambiguation comment (MusicBrainz shows it in grey after the title), or
    else its release group's, title-cased, minus comma-separated parts that are one of the
    texts in `skip` - whole matches, ignoring case, spacing and brackets ("Explicit Lyrics",
    "[E]"), so "Clean Bandit tribute" stays. Empty when there is none."""
    raw = (rel.get("disambiguation") or (rel.get("release-group") or {}).get("disambiguation") or "").strip()
    drop = {_comment_key(s) for s in skip}
    parts = [p.strip() for p in re.split(r"[;]|,(?!\d)", raw)
             if p.strip() and _comment_key(p) not in drop and not (formats and is_audio_format(p))]
    return title_case(", ".join(parts))


def full_title(title: str, comment: str) -> str:
    """"Title (Comment)", unless the title already says it: "Album (Deluxe Edition)" with the
    comment "deluxe edition" stays as it is."""
    if not comment:
        return title
    key = lambda s: re.sub(r"\W+", " ", s).strip().lower()
    return title if key(comment) and key(comment) in key(title) else f"{title} ({comment})"


def build_album(rel: dict, medium: dict, prefs: dict | None = None) -> Album:
    artist, artist_names, artist_ids = _credit(rel.get("artist-credit", []))
    rg = rel.get("release-group") or {}
    infos = rel.get("label-info") or []
    labels = list(dict.fromkeys(n for i in infos if (n := (i.get("label") or {}).get("name"))))
    catalogs = list(dict.fromkeys(c for i in infos if (c := i.get("catalog-number"))))
    sort_of = lambda ac: "".join(c["artist"].get("sort-name", c["artist"]["name"]) + c.get("joinphrase", "") for c in ac)
    tracks = []
    for i, t in enumerate(medium["tracks"], 1):
        rec = t.get("recording") or {}
        ac = t.get("artist-credit") or rec.get("artist-credit") or rel.get("artist-credit", [])
        name, names, ids = _credit(ac)
        tracks.append(Track(i, t.get("title") or rec.get("title", f"Track {i:02d}"), name, names, ids,
                            rec.get("id", ""), t.get("id", ""), sort_of(ac), rec.get("isrcs") or []))
    sort = sort_of(rel.get("artist-credit", []))
    types = [rg.get("primary-type") or "", *(rg.get("secondary-types") or [])]
    return Album(
        mbid=rel["id"], group_id=rg.get("id", ""), artist=artist,
        title=full_title(rel.get("title", ""), comment_for_title(rel, (prefs or {}).get("title_comment_skip") or [],
                                                                (prefs or {}).get("title_comment_skip_formats", True)))
        if (prefs or {}).get("title_comment", True) else rel.get("title", ""),
        artist_sort=sort, artist_ids=artist_ids, artists=artist_names, date=rel.get("date") or "",
        original_date=rg.get("first-release-date") or "",
        country=release_country(rel, prefs or {}) or rel.get("country") or "",
        disambiguation=rel.get("disambiguation") or "",
        status=(rel.get("status") or "").lower(), release_type="; ".join(t.lower() for t in types if t),
        barcode=rel.get("barcode") or "", labels=labels, catalogs=catalogs, asin=rel.get("asin") or "",
        script=(rel.get("text-representation") or {}).get("script") or "", media=medium.get("format") or "",
        disc=medium.get("position", 1), disc_total=len(rel.get("media", [])) or 1, tracks=tracks)


# ---- the identification ladder -----------------------------------------------

def identify(mb: MusicBrainz, cfg: dict, infos: list[FlacInfo], *, discid: str | None,
             toc: Toc | None, forced_release: str | None = None, source: str = "rip",
             chooser=None, repick: bool = False, prefer_discid: bool = False,
             ndiscs: int | None = None) -> Identification:
    """Find the release. source="rip": the disc ID decides, text search is only a fallback.
    source="files": disc ID / TOC hits and a text search on the existing tags are pooled, so a
    Digital Media release can win over the CD pressing the track lengths happen to match.

    chooser: optional callable(ranked, tier, resolve) used when several candidates fit the
    files equally well (see tier_size). It returns (release, medium) to use, or None to skip
    the album. `resolve(mbid)` fetches any release by id and returns (release, medium) or an
    error message. Without a chooser the top-ranked release is used.

    repick: you turned down the release found before, so ignore the files' MUSICBRAINZ_ALBUMID
    tag, gather candidates by disc ID / TOC and tag search, and always ask (chooser required).

    prefer_discid: when the disc ID (real, or computed from CD-style files) is known to
    MusicBrainz, use its releases instead of the MUSICBRAINZ_ALBUMID tag; the tag is used only
    when MusicBrainz knows no release for the disc ID. A forced release id still comes first.

    ndiscs: how many discs of this album the folder holds (existing files); releases with that
    many discs rank higher. None for a rip (one disc in the drive says nothing about the set)."""
    prefs = cfg["musicbrainz"]
    out = Identification()
    ntracks = len(infos)
    durations = [i.seconds for i in infos]
    # When the files are tagged with a release and we look past it (--prefer-discid, or you
    # turned it down), their BARCODE/ASIN tags came from that release and prove nothing about
    # which release they are: only the barcode the disc itself carries (MCN) counts then.
    tag_bound = (prefer_discid or repick) and any(i.tag("MUSICBRAINZ_ALBUMID") for i in infos)
    # the disc's own barcode (MCN) first: it is read from the CD, a BARCODE tag may be anyone's
    keys = ("DISC_MCN", "MCN") if tag_bound else ("DISC_MCN", "MCN", "BARCODE", "UPC")
    barcodes = {i.tag(*keys) for i in infos}
    barcode = barcodes.pop() if len(barcodes) == 1 else ""
    asins = set() if tag_bound else {m.group(0) for i in infos for key in ("ASIN", "WWW", "URL")
                                     for m in [re.search(r"\bB0[0-9A-Z]{8}\b", i.tag(key))] if m}
    asin = asins.pop() if len(asins) == 1 else ""
    want_clean = any(i.tag("RATING", "ITUNESADVISORY").lower() in ("clean", "2") for i in infos)
    isrcs = [i.tag("ISRC").upper().replace("-", "") for i in infos]
    ranker = lambda rels, exact, did: rank(
        rels, ntracks=ntracks, discid=did, durations=durations, prefs=prefs, exact=exact, source=source,
        barcode=barcode, asin=asin, isrcs=isrcs, want_clean=want_clean, ndiscs=ndiscs)

    def resolve(mbid: str):
        """A release the user typed in: it only has to have a disc with the right track count."""
        try:
            found = ranker([mb.release(mbid)], True, discid)
        except NotFound:
            return f"release {mbid} not found on MusicBrainz"
        if not found:
            return f"release {mbid} has no disc with {ntracks} tracks"
        return found[0].release, found[0].medium

    def accept(rels: list[dict], exact: bool, did: str | None, method: str) -> bool:
        ranked = ranker(rels, exact, did)
        out.candidates = report_lines(ranked, total=len(rels), ntracks=ntracks) or out.candidates
        if not ranked:
            return False
        found = (ranked[0].release, ranked[0].medium)
        tier = tier_size(ranked)
        if chooser and (tier > 1 or repick):
            found = chooser(ranked, max(tier, min(len(ranked), 5)) if repick else tier, resolve,
                            heading="Pick the release to use" if repick else None)
            if found is None:
                out.skipped, out.method = True, method
                out.notes.append("you chose to skip this album")
                return True
            if found[0]["id"] != ranked[0].release["id"]:
                out.notes.append("release chosen by you (ripflow recommended the first one listed)")
                known = {c.release["id"] for c in ranked}
                extra = [] if found[0]["id"] in known else ranker([found[0]], True, did)
                out.candidates = report_lines(extra + ranked, total=len(rels) + len(extra),
                                              ntracks=ntracks, chosen=found[0]["id"])
            else:
                out.notes.append("you confirmed the recommended release")
        out.album, out.method = build_album(*found, prefs), method
        if cfg["tags"]["ascii_punctuation"]:
            ascii_punctuation(out.album)
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
        lookup_id = discid or (toc.discid if toc and toc.exact else None)
        for mbid, method, exact in ((forced_release, "forced release id", True),
                                    (tagged_id, "MUSICBRAINZ_ALBUMID tag", False)):
            if not mbid or (repick and method.startswith("MUSICBRAINZ")):
                continue
            if prefer_discid and lookup_id and method.startswith("MUSICBRAINZ"):
                try:                          # the exact disc only, no fuzzy TOC match
                    data = mb.by_disc(lookup_id, None)
                except NotFound:
                    data = {}
                rels = (data.get("releases") or []) if data.get("id") == lookup_id else []
                if rels and accept(rels, True, lookup_id, "disc ID (preferred over the MUSICBRAINZ_ALBUMID tag)"):
                    if out.album and out.album.mbid != mbid:
                        out.notes.append(f"the MUSICBRAINZ_ALBUMID tag had {mbid}; the disc ID gave another release")
                    return out
                out.notes.append(f"disc ID {lookup_id}: no release on MusicBrainz - using the "
                                 "MUSICBRAINZ_ALBUMID tag")
            try:
                if accept([mb.release(mbid)], exact, discid, method):
                    return out
                out.notes.append(f"{method} {mbid}: track count/lengths do not match these files")
            except NotFound:
                out.notes.append(f"{method} {mbid}: not found on MusicBrainz")

        pool: dict[str, dict] = {}
        attached = None                      # disc id, if MusicBrainz knows this exact disc
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
        if source == "rip" and not repick and pool and accept(list(pool.values()), bool(attached), attached,
                                               "disc ID" if attached else "fuzzy TOC (track lengths)"):
            return out

        artists = {i.tag("ALBUMARTIST", "ALBUM ARTIST", "ARTIST") for i in infos}
        albums = {i.tag("ALBUM") for i in infos}
        artist = hint_artist or (artists.pop() if len(artists) == 1 else "")
        if pool and not attached and artist and artist != cfg["naming"]["unknown_artist"]:
            # a lookup by track lengths knows nothing of the artist: drop other artists' releases
            before = len(pool)
            pool = {k: r for k, r in pool.items()
                    if similar(_credit(r.get("artist-credit", []))[0], artist) >= 0.8}
            if len(pool) < before:
                out.notes.append(f"fuzzy TOC: dropped {before - len(pool)} release(s) by other artists")
        album = hint_album or (albums.pop() if len(albums) == 1 else "")
        if album and artist != cfg["naming"]["unknown_artist"]:
            # the tag may already carry a disambiguation comment ripflow added: "X (Red Album)"
            bare = strip_edition(album, all_brackets=True) or album
            hits = [h for h in mb.search(artist, album, ntracks)
                    if max(similar(h.get("title", ""), album), similar(h.get("title", ""), bare)) >= 0.85
                    and h["id"] not in pool][:15]
            full = [mb.release(h["id"]) for h in hits]
            if artist:
                full = [r for r in full if similar(_credit(r.get("artist-credit", []))[0], artist) >= 0.8]
            pool.update((r["id"], r) for r in full)
            out.notes.append(f"tag search ({artist or '?'} / {album}): {len(full)} more release(s)")
        if pool and accept(list(pool.values()), False, attached,
                           "disc/TOC lookup + tag search, ranked together"):
            return out
        if repick and chooser:               # nothing found: a pasted release id is the way out
            found = chooser([], 0, resolve, heading="MusicBrainz found no other release for these files")
            if found is None:
                out.skipped, out.method = True, "your choice"
                out.notes.append("you chose to skip this album")
            else:
                accept([found[0]], True, attached, "release id you entered")
            return out
    except NetworkError as e:
        out.offline = True
        out.notes.append(f"MusicBrainz unreachable: {e}")
        log.warning("MusicBrainz unreachable: %s", e)
    return out
